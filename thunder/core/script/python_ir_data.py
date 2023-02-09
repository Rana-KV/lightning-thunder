import dis

# this is Python 3.10 specific for the time being.

#  *  0 -- when not jump
#  *  1 -- when jump
#  * -1 -- maximal

# input, output probably would be smart to highlight inplace mods and global side effects
# (e.g. setup_annotations, import_star), too
fixed_stack_effects_detail = {
    "NOP": (0, 0),
    "EXTENDED_ARG": (0, 0),
    # Stack manipulation
    "POP_TOP": (1, 0),
    "ROT_TWO": (2, 2),
    "ROT_THREE": (3, 3),
    "ROT_FOUR": (4, 4),
    "DUP_TOP": (1, 2),
    "DUP_TOP_TWO": (2, 4),
    # Unary operators
    "UNARY_POSITIVE": (1, 1),
    "UNARY_NEGATIVE": (1, 1),
    "UNARY_NOT": (1, 1),
    "UNARY_INVERT": (1, 1),
    "SET_ADD": (2, 1),  # these leave the container on the stack
    "LIST_APPEND": (2, 1),
    "MAP_ADD": (3, 1),
    # Binary operators
    "BINARY_POWER": (2, 1),
    "BINARY_MULTIPLY": (2, 1),
    "BINARY_MATRIX_MULTIPLY": (2, 1),
    "BINARY_MODULO": (2, 1),
    "BINARY_ADD": (2, 1),
    "BINARY_SUBTRACT": (2, 1),
    "BINARY_SUBSCR": (2, 1),
    "BINARY_FLOOR_DIVIDE": (2, 1),
    "BINARY_TRUE_DIVIDE": (2, 1),
    "INPLACE_FLOOR_DIVIDE": (2, 1),
    "INPLACE_TRUE_DIVIDE": (2, 1),
    "INPLACE_ADD": (2, 1),
    "INPLACE_SUBTRACT": (2, 1),
    "INPLACE_MULTIPLY": (2, 1),
    "INPLACE_MATRIX_MULTIPLY": (2, 1),
    "INPLACE_MODULO": (2, 1),
    "BINARY_LSHIFT": (2, 1),
    "BINARY_RSHIFT": (2, 1),
    "BINARY_AND": (2, 1),
    "BINARY_XOR": (2, 1),
    "BINARY_OR": (2, 1),
    "INPLACE_POWER": (2, 1),
    "INPLACE_LSHIFT": (2, 1),
    "INPLACE_RSHIFT": (2, 1),
    "INPLACE_AND": (2, 1),
    "INPLACE_XOR": (2, 1),
    "INPLACE_OR": (2, 1),
    "STORE_SUBSCR": (3, 0),
    "DELETE_SUBSCR": (2, 0),
    "GET_ITER": (1, 1),
    "PRINT_EXPR": (1, 0),
    "LOAD_BUILD_CLASS": (0, 1),
    "RETURN_VALUE": (1, 0),
    "IMPORT_STAR": (1, 0),
    "SETUP_ANNOTATIONS": (0, 0),
    "YIELD_VALUE": (1, 1),  # I think
    "YIELD_FROM": (2, 1),  # I am very unsure
    "POP_BLOCK": (0, 0),
    "POP_EXCEPT": (3, 0),
    "STORE_NAME": (1, 0),
    "DELETE_NAME": (0, 0),
    "STORE_ATTR": (2, 0),
    "DELETE_ATTR": (1, 0),
    "STORE_GLOBAL": (1, 0),
    "DELETE_GLOBAL": (0, 0),
    "LOAD_CONST": (0, 1),
    "LOAD_NAME": (0, 1),
    "LOAD_ATTR": (1, 1),
    "COMPARE_OP": (2, 1),
    "IS_OP": (2, 1),
    "CONTAINS_OP": (2, 1),
    "JUMP_IF_NOT_EXC_MATCH": (2, 0),
    "IMPORT_NAME": (2, 1),
    "IMPORT_FROM": (1, 2),
    # Jumps
    "JUMP_FORWARD": (0, 0),
    "JUMP_ABSOLUTE": (0, 0),
    "POP_JUMP_IF_FALSE": (1, 0),
    "POP_JUMP_IF_TRUE": (1, 0),
    "LOAD_GLOBAL": (0, 1),
    "RERAISE": (3, 0),
    "WITH_EXCEPT_START": (7, 8),  # ??!?
    "LOAD_FAST": (0, 1),
    "STORE_FAST": (1, 0),
    "DELETE_FAST": (0, 0),
    # Closures
    "LOAD_CLOSURE": (0, 1),
    "LOAD_DEREF": (0, 1),
    "LOAD_CLASSDEREF": (0, 1),
    "STORE_DEREF": (1, 0),
    "DELETE_DEREF": (0, 0),
    # Iterators and generators
    "GET_AWAITABLE": (1, 1),
    "BEFORE_ASYNC_WITH": (1, 2),
    "GET_AITER": (1, 1),
    "GET_ANEXT": (1, 2),
    "GET_YIELD_FROM_ITER": (1, 1),
    "END_ASYNC_FOR": (7, 0),
    "LOAD_METHOD": (1, 2),
    "LOAD_ASSERTION_ERROR": (0, 1),
    "LIST_TO_TUPLE": (1, 1),
    "GEN_START": (1, 0),
    "LIST_EXTEND": (2, 1),
    "SET_UPDATE": (2, 1),
    "DICT_MERGE": (2, 1),
    "DICT_UPDATE": (2, 1),
    "COPY_DICT_WITHOUT_KEYS": (2, 2),
    "MATCH_CLASS": (3, 2),
    "GET_LEN": (1, 2),
    "MATCH_MAPPING": (1, 2),
    "MATCH_SEQUENCE": (1, 2),
    "MATCH_KEYS": (2, 4),
}


def stack_effect_detail(opname: str, oparg: int, *, jump: bool = False):
    if opname in fixed_stack_effects_detail:
        return fixed_stack_effects_detail[opname]
    elif opname == "ROT_N":
        return (oparg, oparg)
    elif opname in {"BUILD_TUPLE", "BUILD_LIST", "BUILD_SET", "BUILD_STRING"}:
        return (oparg, 1)
    elif opname == "BUILD_MAP":
        return (2 * oparg, 1)
    elif opname == "BUILD_CONST_KEY_MAP":
        return (oparg + 1, 1)
    elif opname in {"JUMP_IF_TRUE_OR_POP", "JUMP_IF_FALSE_OR_POP"}:
        return (1, 1) if jump else (1, 0)
    elif opname == "SETUP_FINALLY":
        return (0, 6) if jump else (0, 0)
    # Exception handling
    elif opname == "RAISE_VARARGS":
        return (oparg, 0)
    # Functions and calls
    elif opname == "CALL_FUNCTION":
        return (oparg + 1, 1)
    elif opname == "CALL_METHOD":
        return (oparg + 2, 1)
    elif opname == "CALL_FUNCTION_KW":
        return (oparg + 2, 1)
    elif opname == "CALL_FUNCTION_EX":
        return (2 + ((oparg & 0x01) != 0), 1)
    elif opname == "MAKE_FUNCTION":
        return (
            2 + ((oparg & 0x01) != 0) + ((oparg & 0x02) != 0) + ((oparg & 0x04) != 0) + ((oparg & 0x08) != 0),
            1,
        )
    elif opname == "BUILD_SLICE":
        return (oparg, 1)
    elif opname == "SETUP_ASYNC_WITH":
        return (1, 6) if jump else (0, 0)  # ??
    elif opname == "FORMAT_VALUE":
        return (2, 1) if ((oparg & 0x04) != 0) else (1, 1)
    elif opname == "UNPACK_SEQUENCE":
        return (1, oparg)
    elif opname == "UNPACK_EX":
        return (1, (oparg & 0xFF) + (oparg >> 8) + 1)
    elif opname == "FOR_ITER":
        return (1, 0) if jump else (1, 2)
    else:
        raise ValueError(f"Invalid opname {opname}")


jump_instructions = set(dis.hasjabs) | set(dis.hasjrel)

unconditional_jump_names = {"JUMP_ABSOLUTE", "JUMP_FORWARD", "JUMP_BACKWARD", "JUMP_BACKWARD_NO_INTERRUPT"}