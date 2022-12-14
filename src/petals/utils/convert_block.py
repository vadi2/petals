"""
Tools for converting transformer blocks, applying quantization and/or tensor parallelism
"""
from typing import Sequence

import bitsandbytes as bnb
import tensor_parallel as tp
import torch
import torch.nn as nn

from petals.bloom.block import WrappedBloomBlock
from petals.utils.linear8bitlt_patch import CustomLinear8bitLt


def replace_8bit_linear(module: nn.Module, threshold=6.0) -> nn.Module:
    """
    A helper function to convert all `torch.nn.Linear` modules to `bnb.nn.Linear8bit` modules from the `bitsandbytes`
    library. This will enable running your models using mixed int8 precision as described by the paper `GPT3.int8():
    8-bit Matrix Multiplication for Transformers at Scale`. Make sure `bitsandbytes` compiled with the correct CUDA
    version of your hardware is installed before running this function. `pip install -i https://test.pypi.org/simple/
    bitsandbytes-cudaXXX` with `XXX` is your CUDA version (e.g., 11.6 = 116)
    The function will be run recursively and replace all `torch.nn.Linear` modules except for the `lm_head` and 'score' that should
    be kept as a `torch.nn.Linear` module.
    Parameters:
        module (`torch.nn.Module`):
            Input model or `torch.nn.Module` as the function is run recursively.
        threshold (`float`, *optional*):
            `int8_threshold` for outlier detection as described in the formentioned paper. This parameters is set to
            `6.0` as described by the paper.
    """
    for n, module in module.named_children():
        if len(list(module.children())) > 0:
            replace_8bit_linear(module, threshold)

        if isinstance(module, torch.nn.Linear) and n not in ["lm_head", "score"]:
            module._modules[n] = CustomLinear8bitLt(
                module.in_features,
                module.out_features,
                module.bias is not None,
                has_fp16_weights=False,
                threshold=threshold,
            )
            module._modules[n].weight = bnb.nn.Int8Params(
                module.weight.data, requires_grad=False, has_fp16_weights=False
            ).to(module.weight.dtype)
            module._modules[n].bias = module.bias
    return module


def make_tensor_parallel(block: WrappedBloomBlock, devices: Sequence[torch.device], output_device: torch.device):
    assert isinstance(block, WrappedBloomBlock)
    tp_config = tp.Config(
        state_rules={
            ".*self_attention\.query_key_value\.(weight|bias)": "split 0",
            ".*self_attention\.dense\.(weight|bias)": "split 0",
            ".*mlp\.dense_h_to_4h\.(weight|bias)": "split 0",
            ".*mlp\.dense_4h_to_h\.weight": "split 1",
            ".*mlp\.dense_4h_to_h\.bias": "scale",
        },
        input_rules={},
        output_rules={
            ".*self_attention\.query_key_value": {0: "gather -1"},
            ".*self_attention\.dense": {0: "gather -1"},
            ".*mlp\.dense_4h_to_h$": {0: "sum"},
        },
        attr_rules={},
    )
    # note: this config can be further improved to make it (slightly) faster and reduce memory usage
    # If num gpus is a power of two (2, 4, 8), it is better to split attention by heads and all-reduce once at the end
    # This requires some modifications to layer_past, alibi and num_heads, and the way new_layer_past is processed
    # This optimization is not implemented in the current PR because it was tested (rigorously) with the current config
    return tp.TensorParallel(block, devices, config=tp_config, output_device=output_device)
