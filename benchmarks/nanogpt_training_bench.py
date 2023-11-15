"""This is a modified bench.py from https://github.com/karpathy/nanogpt.

MIT License

Copyright (c) 2022 Andrej Karpathy

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""
import argparse
from contextlib import nullcontext
import os
import time

import torch
import torch.distributed as torch_dist

import thunder
from thunder.tests.nanogpt_model import GPT, GPTConfig

_configs = {
    "gpt2": dict(n_layer=12, n_head=12, n_embd=768),  # 124M params
    "gpt2-medium": dict(n_layer=24, n_head=16, n_embd=1024),  # 350M params
    "gpt2-large": dict(n_layer=36, n_head=20, n_embd=1280),  # 774M params
    "gpt2-xl": dict(n_layer=48, n_head=25, n_embd=1600),  # 1558M params
}

parser = argparse.ArgumentParser(
    description="Use `torchrun` to enable `ddp`",
    formatter_class=argparse.ArgumentDefaultsHelpFormatter,
)
parser.add_argument("--compile-mode", default="thunder", choices=("thunder", "torch"))
parser.add_argument("--dtype", default="float32", choices=("float32", "float16", "bfloat16"))
parser.add_argument("--print-loss", action="store_true", help="Set this `True` to print loss every step")
parser.add_argument("--profile", action="store_true")
parser.add_argument("--nsys-profile", action="store_true")
parser.add_argument("--model", default="gpt2-medium", choices=tuple(_configs.keys()))
parser.add_argument("--bucket-size-in-mb", type=float, default=25.0)
parser.add_argument("--seq-len", type=int, default=128)
parser.add_argument("--dump-extrace", action="store_true")
parser.add_argument("--skip-torch-compile", action="store_true")
parser.add_argument("--delay-allreduce", action="store_true")
args = parser.parse_args()
# -----------------------------------------------------------------------------
config = args.model
batch_size = 16
seq_len = args.seq_len
bias = False
seed = 1337
device = "cuda"  # examples: 'cpu', 'cuda', 'cuda:0', 'cuda:1', etc.
#'bfloat16' if torch.cuda.is_available() and torch.cuda.is_bf16_supported() else 'float16'
dtype = args.dtype  # 'float32' or 'bfloat16' or 'float16'
compile_mode = args.compile_mode
print_loss = args.print_loss
use_ddp = False
bucket_size_in_mb: float = args.bucket_size_in_mb
if args.dump_extrace:
    assert compile_mode == "thunder"
# -----------------------------------------------------------------------------

world_size, local_rank, pg = None, None, None
if "WORLD_SIZE" in os.environ and "LOCAL_RANK" in os.environ:
    torch_dist.init_process_group(backend="nccl")
    world_size = int(os.environ["WORLD_SIZE"])
    local_rank = int(os.environ["LOCAL_RANK"])
    pg = torch_dist.distributed_c10d._get_default_group()
    device = torch.device("cuda", local_rank)
    use_ddp = True
    if local_rank == 0:
        print("Distributed NanoGPT bench")

if args.skip_torch_compile:
    assert use_ddp

torch.manual_seed(seed)
torch.cuda.manual_seed(seed)
torch.backends.cuda.matmul.allow_tf32 = True  # allow tf32 on matmul
torch.backends.cudnn.allow_tf32 = True  # allow tf32 on cudnn

x = torch.randint(50304, (batch_size, seq_len), device=device)
y = torch.randint(50304, (batch_size, seq_len), device=device)
get_batch = lambda split: (x, y)

gptconf = GPTConfig(
    block_size=1024,  # how far back does the model look? i.e. context size
    vocab_size=50304,  # number of tokens
    dropout=0.1,
    **_configs[config],
)
model = GPT(gptconf)
ptdtype = {"float32": torch.float32, "bfloat16": torch.bfloat16, "float16": torch.float16}[dtype]
model.to(device=device).to(dtype=ptdtype)
optimizer_ctor = model.configure_optimizers
if use_ddp:
    if compile_mode == "thunder":
        from thunder.distributed import ddp

        model = ddp(
            model,
            rank=local_rank,
            broadcast_from=0,
            process_group=pg,
            bucket_size_in_mb=bucket_size_in_mb,
        )
    else:
        model = torch.nn.parallel.distributed.DistributedDataParallel(
            model,
            device_ids=[local_rank],
            bucket_cap_mb=bucket_size_in_mb,
        )

optimizer = optimizer_ctor(weight_decay=1e-2, learning_rate=1e-4, betas=(0.9, 0.95), device_type="cuda")

if compile_mode == "torch":
    if not (args.skip_torch_compile or bucket_size_in_mb <= 0):
        print("Compiling model using torch.compile...")
        model = torch.compile(model)  # pytorch 2.0
elif compile_mode == "thunder":
    print("Compiling model using thunder.compile...")
    model = thunder.compile(model)
else:
    raise ValueError(f"Unknown compile_mode: {compile_mode}")

save_files = not (torch.distributed.is_initialized() and torch.distributed.get_rank() != 0)
dir_for_outputs = (
    (
        f"./thunder_traces/{config}_{args.dtype}_seq-{args.seq_len}"
        f"{'_ddp_bucket_size-' + str(bucket_size_in_mb) if use_ddp else ''}"
        f"{'_delayed_allreduce' if use_ddp and args.delay_allreduce else ''}"
    )
    if save_files
    else None
)
if save_files and not os.path.exists(dir_for_outputs):
    os.makedirs(dir_for_outputs)

# simple benchmarking
context = nullcontext()
losses: list[torch.Tensor] = []
put_nvtx_markers: bool = False
for stage, num_steps in enumerate([10, 20]):  # burnin, then benchmark
    if stage == 1:
        if args.profile:
            context = torch.profiler.profile(
                record_shapes=True,
                profile_memory=True,
                with_modules=True,
                on_trace_ready=torch.profiler.tensorboard_trace_handler(
                    os.path.join(dir_for_outputs, f"rank_{torch.distributed.get_rank()}"),
                ),
            )
        if args.nsys_profile:
            put_nvtx_markers = True
            torch.cuda.profiler.start()
    torch.cuda.synchronize()
    t0 = time.time()
    with context:
        for k in range(num_steps):
            if put_nvtx_markers:
                torch.cuda.nvtx.range_push(f"iter_{k}")

            X, Y = get_batch("train")

            if put_nvtx_markers:
                torch.cuda.nvtx.range_push("forward")
            logits, loss = model(X, Y)
            if put_nvtx_markers:
                torch.cuda.nvtx.range_pop()

            if put_nvtx_markers:
                torch.cuda.nvtx.range_push("backward")
            loss.backward()
            if put_nvtx_markers:
                torch.cuda.nvtx.range_pop()

            if put_nvtx_markers:
                torch.cuda.nvtx.range_push("optimizer.step")
            optimizer.step()
            if put_nvtx_markers:
                torch.cuda.nvtx.range_pop()

            if put_nvtx_markers:
                torch.cuda.nvtx.range_push("optimizer.zero_grad")
            optimizer.zero_grad(set_to_none=True)
            if put_nvtx_markers:
                torch.cuda.nvtx.range_pop()

            if print_loss:
                lossf = loss.item()
                print(f"{k}/{num_steps} loss: {lossf:.4f}")
            elif stage == 1:
                losses.append(loss.detach())

            if put_nvtx_markers:
                torch.cuda.nvtx.range_pop()  # iter
    torch.cuda.synchronize()
    t1 = time.time()
    dt = t1 - t0
    if stage == 1:
        if local_rank is None:
            print(f"time per iteration: {dt/num_steps*1000:.4f}ms")
        else:
            print(f"time per iteration at rank{local_rank}: {dt/num_steps*1000:.4f}ms")
if args.nsys_profile:
    torch.cuda.profiler.stop()
if losses:
    for i, loss in enumerate(losses):
        if use_ddp:
            with torch.inference_mode():
                torch_dist.all_reduce(loss, op=torch_dist.distributed_c10d.ReduceOp.AVG)
        if local_rank in (None, 0):
            print(f"# iteration {i}, loss: {loss.item():.4f}")
if args.profile:
    if local_rank == 0:
        key_averages = context.key_averages()
        allreduce_elements = [e for e in key_averages if "all_reduce" in e.key]
        for e in allreduce_elements:
            print(f"# No. of occurrences {e.count} {e}")
        print(context.key_averages().table(sort_by="cuda_time_total"))
if args.dump_extrace and save_files:
    preamble = f"### {config=}, {dtype=}, {seq_len=}, {use_ddp=}, {bucket_size_in_mb=}, {args.delay_allreduce=}\n"
    fwd_traces, bwd_traces = thunder.last_traces(model)

    with open(os.path.join(dir_for_outputs, "fwd_trace.py"), "w") as f:
        f.write(preamble + str(fwd_traces[-1]))
    with open(os.path.join(dir_for_outputs, "bwd_trace.py"), "w") as f:
        f.write(preamble + str(bwd_traces[-1]))
