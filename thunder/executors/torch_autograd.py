from dataclasses import replace
from functools import wraps, partial
from inspect import signature
from itertools import groupby
from typing import Union, Any, Tuple, Optional
from collections.abc import Callable

import torch

from thunder.core.proxies import TensorProxy, FutureTensorProxy, variableify
from thunder.core.prims import PrimIDs
import thunder.core.utils as utils
from thunder.core.pytree import tree_flatten, tree_unflatten
from thunder.core.transform_common import replace_redundant_inputs
from thunder.core.trace import TraceCtx, set_tracectx, reset_tracectx, from_trace
from thunder.core.symbol import Symbol, BoundSymbol
import thunder.distributed.prims as dist_prims
import thunder.torch as ltorch


class ThunderFunction(torch.autograd.Function):
    @staticmethod
    def get_forward_backward_splitter(func, compile_config, compile_data, compile_stats):
        from thunder import trace
        from thunder.executors.passes import transform_for_execution
        from thunder.executors.passes import del_last_used
        from thunder.core.rematerialization import rematerialize_forward_and_backward
        from thunder.core.transforms import forward_and_backward_from_trace
        from thunder.cudagraphs import CUDAGraphExecutor
        from thunder.distributed.utils import sort_waits, sort_data_parallel_syncs

        def make_trace(func):
            return partial(trace(compile_data=compile_data, inline_trace=False, insert_ddp_syncs=True), func)

        def split_forward_backward(*args, **kwargs):
            # NOTE: This function is rather slow, so it's intended to be used
            # behind a cache.
            ba = signature(func).bind(*args, **kwargs)
            ba.apply_defaults()
            args, kwargs = ba.args, ba.kwargs
            flat_args, _ = tree_flatten((args, kwargs))
            tensor_cls = (torch.Tensor, TensorProxy)
            requires_grad_mask = tuple(isinstance(arg, tensor_cls) and arg.requires_grad for arg in flat_args)
            # If none of the inputs require gradients, raise an error
            if not any(requires_grad_mask):
                raise RuntimeError(
                    "PyTorch's Autograd interface requires at least one tensor input with requires_grad=True"
                )

            primal_trace = make_trace(func)(*args, **kwargs)
            primal_trace = sort_data_parallel_syncs(primal_trace)

            # torch.autograd.Function doesn't support non-flat outputs, the
            # grads wouldn't be propagated and backward receives None for each
            # non-flat non-tensor output. The output must also be a flat tuple,
            # not any other container type. So we need to flatten the outputs of
            # the forward trace and inputs of the backward trace.
            fw_trace, bw_trace = forward_and_backward_from_trace(primal_trace, torch_autograd=True)

            # Update the backward trace to only compute gradients for the
            # inputs that require gradients
            assert bw_trace.bound_symbols[-1].sym.id == PrimIDs.RETURN
            filtered_grads = tuple(
                (arg_grad if requires_grad else None)
                for arg_grad, requires_grad in utils.safe_zip(bw_trace.bound_symbols[-1].args[0], requires_grad_mask)
            )

            # autograd.Function.backward expects a flat tuple of gradients
            bw_trace.bound_symbols[-1] = replace(bw_trace.bound_symbols[-1], args=(filtered_grads,))

            # Now we can run the optimization passes on the forward trace
            # TODO Restore request for no rematerialization
            fw_extrace = transform_for_execution(
                fw_trace,
                executors_list=compile_config.get("executors_list", None),
            )

            # Some of the optimization passes change proxies in the trace and
            # any change in the forward trace must be reflected in the backward
            # trace.
            original_bw_saved_tensors_for_backward = bw_trace.args[0][0]
            new_fw_saved_tensors_for_backward = fw_extrace.output[1][0]
            swap_map = {
                variableify(x): y
                for x, y in zip(original_bw_saved_tensors_for_backward, new_fw_saved_tensors_for_backward)
                if variableify(x) != variableify(y)
            }
            new_bsyms = replace_redundant_inputs(swap_map, bw_trace.bound_symbols)
            # replace_redundant_inputs doesn't replace the output of
            # UNPACK_SEQUENCE so we do it manually. Here we have certain
            # assumptions about the structure of the backward trace.
            assert bw_trace.bound_symbols[0].sym.id == PrimIDs.UNPACK_TRIVIAL
            assert bw_trace.bound_symbols[0].kwargs["name"] == "saved_for_backward"
            assert bw_trace.bound_symbols[4].sym.id == PrimIDs.UNPACK_SEQUENCE
            assert bw_trace.bound_symbols[4].args[0].name == "C0"
            new_bsyms[4] = new_bsyms[4].from_bsym_swap_proxies(
                swap_map,
                skip_inputs=False,
                skip_output=False,
                skip_subsymbols=False,
            )
            bw_trace.bound_symbols = new_bsyms

            # Now we can run the optimization passes on the backward trace
            # TODO Restore request for no rematerialization
            bw_extrace = transform_for_execution(
                bw_trace,
                executors_list=compile_config.get("executors_list", None),
            )

            fw_extrace, bw_extrace = rematerialize_forward_and_backward(fw_extrace, bw_extrace)

            # We need to sort the waits in the backward trace to overlap
            # computation with communication
            bw_extrace = sort_waits(bw_extrace)

            fw_extrace = del_last_used(fw_extrace)

            bw_extrace = del_last_used(bw_extrace)

            if compile_stats is not None:
                compile_stats.primal_trace = primal_trace
                compile_stats.forward_last_traces = [fw_extrace]
                compile_stats.backward_last_traces = [bw_extrace]

                if compile_data.use_cudagraphs or compile_config.get("use_cudagraphs", False):
                    fw = CUDAGraphExecutor(
                        fw_extrace.python_callable(), num_constant_args=compile_data.num_constant_args
                    )
                    bw = CUDAGraphExecutor(bw_extrace.python_callable(), num_constant_args=len(bw_extrace.args[0][0]))
                    return fw, bw

            return fw_extrace.python_callable(), bw_extrace.python_callable()

        return split_forward_backward

    @staticmethod
    def forward(ctx, compiled_backward, saved_tensors, saved_other, flat_output, *flat_args):
        # Here we just propagate the tensors through the autograd graph
        ctx.saved_other = saved_other
        ctx.compiled_backward = compiled_backward

        # We must save tensors using ctx.save_for_backward
        ctx.save_for_backward(*saved_tensors)
        return flat_output

    @staticmethod
    @torch.autograd.function.once_differentiable
    def backward(ctx, *args):
        grads = ctx.compiled_backward((ctx.saved_tensors, ctx.saved_other), args)
        return (None, None, None, None, *grads)


def thunder_backward(*, compile_data=None, compile_stats=None, **compile_config):
    """Decorator to wrap a Thunder function for use with PyTorch autograd.

    Args:
        thunder_func: A Thunder function.

    Returns:
        A wrapped function that can be used with PyTorch autograd.

    Example:
    >>> import torch
    >>> import thunder.clang as clang
    >>> from thunder.executors.torchex import thunder_backward
    >>> @thunder_backward()
    ... def func(a, b):
    ...     c = a + b
    ...     d = c * b
    ...     e = clang.sin(d) + clang.cos(c)
    ...     return e
    >>> a = torch.randn(3, device="cuda", requires_grad=True)
    >>> b = torch.randn(3, device="cuda", requires_grad=True)
    >>> c = func(a, b)
    >>> print(c)
    >>> sum(c).sum().backward()
    >>> print(f"a.grad: {a.grad}")
    >>> print(f"b.grad: {b.grad}")
    """

    compile_config = compile_config | {"disable_preprocessing": True} | {"disable_torch_autograd_support": True}

    def decorator(thunder_func):
        from thunder import compile

        # Compile's caching only works for many calls to the same compiled function
        # It does not work if the same function is compiled many times, so we must
        # decorate the augmented forward pass once with compile once and reuse it
        split_fw_bw = ThunderFunction.get_forward_backward_splitter(
            thunder_func, compile_config, compile_data, compile_stats
        )
        compiled_split_fw_bw = compile(
            split_fw_bw,
            **compile_config,
        )
        sig = signature(thunder_func)

        @wraps(thunder_func)
        def wrapper(*args, **kwargs):
            # Fetch the compiled forward and backward functions using the
            # compiled function cache
            compiled_forward, compiled_backward = compiled_split_fw_bw(*args, **kwargs)

            # Compiled forward function currently doesn't support positional
            # arguments passed as kwargs, so we must bind them here
            ba = sig.bind(*args, **kwargs)
            args, kwargs = ba.args, ba.kwargs

            # Run the compiled forward function
            data_for_autograd, (saved_tensors, saved_other) = compiled_forward(*args, **kwargs)

            # Connect produced tensors with PyTorch's autograd graph
            ThunderFunction.apply(
                compiled_backward,
                saved_tensors,
                saved_other,
                data_for_autograd["flat_output"],
                *data_for_autograd["flat_args"],
            )
            return data_for_autograd["output"]

        return wrapper

    return decorator


if torch.distributed.is_available():
    from torch.distributed.distributed_c10d import ProcessGroup

    def insert_bsym_to_allreduce_grads(
        backward_trace: TraceCtx,
        process_group: ProcessGroup | None,
    ) -> TraceCtx:
        """Insert :class:`BoundSymbol`s of pre-averaging, async all_reduce, and wait.

        Args:
            joint_trace: A trace representing backward.
            process_group:
        """
        from torch.distributed.distributed_c10d import _get_default_group
        from thunder.core import prims
        from thunder.core.transforms import visitor_transform, VISIT_TYPE

        # NOTE(crcrpar): To do "pre-averaging" to mitigate grad overflow,
        # we need to know the world size of ddp.
        pg: ProcessGroup = _get_default_group() if process_group is None else process_group
        world_size = float(pg.size())
        gradients, orig_grads_spec = tree_flatten(backward_trace.output)
        grad_to_future = utils.ProxyDict()
        for grad in gradients:
            if not isinstance(grad, TensorProxy):
                continue
            grad_to_future[grad] = True

        class AllReduceGradVisitor:
            def __init__(self):
                self.future_tensor_proxies: list[FutureTensorProxy] = []

            def __call__(self, bsym: BoundSymbol) -> None:
                sym: Symbol = bsym.sym
                if sym.id == PrimIDs.RETURN:
                    prims.python_return(
                        *[
                            dist_prims.wait(grad_to_future[grad]) if isinstance(grad, TensorProxy) else None
                            for grad in gradients
                        ]
                    )
                    return VISIT_TYPE.REPLACE
                grads_of_bsym = tuple(t for t in bsym._flat_outs if isinstance(t, TensorProxy) and t in grad_to_future)
                if len(grads_of_bsym) == 0:
                    # NOTE(crcrpar): Wouldn't `VISIT_TYPE.NOOP` be more lucid?
                    return VISIT_TYPE.INSERT_AFTER
                for grad in grads_of_bsym:
                    preaveraged = ltorch.true_divide(grad, world_size)
                    future = ltorch.all_reduce(preaveraged, group=pg, async_op=True)
                    grad_to_future[grad] = future

                return VISIT_TYPE.INSERT_AFTER

        backward_trace_with_grads_allreduced = visitor_transform(
            trace_from=backward_trace,
            visit=AllReduceGradVisitor(),
            provenance="All-reduce gradients tranform",
        )
        return backward_trace_with_grads_allreduced