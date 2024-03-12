"""The pass that attaches logit processor functions to the IRModule."""

import tvm
from tvm import IRModule
from tvm.script import tir as T


@tvm.transform.module_pass(opt_level=0, name="AttachLogitProcessFunc")
class AttachLogitProcessFunc:  # pylint: disable=too-few-public-methods
    """Attach logit processing TIR functions to IRModule."""

    def transform_module(self, mod: IRModule, _ctx: tvm.transform.PassContext) -> IRModule:
        """Entrypoint"""
        mod = mod.clone()
        mod["apply_logit_bias_inplace"] = _apply_logit_bias_inplace
        mod["apply_penalty_inplace"] = _apply_penalty_inplace
        mod["apply_bitmask_inplace"] = _apply_bitmask_inplace
        return mod


@T.prim_func
def _apply_logit_bias_inplace(
    var_logits: T.handle,
    var_pos2seq_id: T.handle,
    var_token_ids: T.handle,
    var_logit_bias: T.handle,
) -> None:
    """Function that applies logit bias in place."""
    T.func_attr(
        {"global_symbol": "apply_logit_bias_inplace", "tir.noalias": True, "tir.is_scheduled": True}
    )
    batch_size = T.int32(is_size_var=True)
    vocab_size = T.int32(is_size_var=True)
    num_token = T.int32(is_size_var=True)
    logits = T.match_buffer(var_logits, (batch_size, vocab_size), "float32")
    # seq_ids
    pos2seq_id = T.match_buffer(var_pos2seq_id, (num_token,), "int32")
    token_ids = T.match_buffer(var_token_ids, (num_token,), "int32")
    logit_bias = T.match_buffer(var_logit_bias, (num_token,), "float32")

    for p0 in T.thread_binding(0, (num_token + 1023) // 1024, "blockIdx.x"):
        for p1 in T.thread_binding(0, 1024, "threadIdx.x"):
            with T.block("block"):
                vp = T.axis.spatial(num_token, p0 * 1024 + p1)
                T.where(p0 * 1024 + p1 < num_token)
                logits[pos2seq_id[vp], token_ids[vp]] += logit_bias[vp]


@T.prim_func
def _apply_penalty_inplace(  # pylint: disable=too-many-arguments,too-many-locals
    var_logits: T.handle,
    var_seq_ids: T.handle,
    var_pos2seq_id: T.handle,
    var_token_ids: T.handle,
    var_token_cnt: T.handle,
    var_penalties: T.handle,
) -> None:
    """Function that applies penalties in place."""
    T.func_attr(
        {"global_symbol": "apply_penalty_inplace", "tir.noalias": True, "tir.is_scheduled": True}
    )
    batch_size = T.int32(is_size_var=True)
    vocab_size = T.int32(is_size_var=True)
    num_token = T.int32(is_size_var=True)
    num_seq = T.int32(is_size_var=True)
    logits = T.match_buffer(var_logits, (batch_size, vocab_size), "float32")
    seq_ids = T.match_buffer(var_seq_ids, (num_seq,), "int32")
    pos2seq_id = T.match_buffer(var_pos2seq_id, (num_token,), "int32")
    token_ids = T.match_buffer(var_token_ids, (num_token,), "int32")
    token_cnt = T.match_buffer(var_token_cnt, (num_token,), "int32")
    penalties = T.match_buffer(var_penalties, (num_seq, 3), "float32")

    for p0 in T.thread_binding(0, (num_token + 1023) // 1024, "blockIdx.x"):
        for p1 in T.thread_binding(0, 1024, "threadIdx.x"):
            with T.block("block"):
                vp = T.axis.spatial(num_token, p0 * 1024 + p1)
                T.where(p0 * 1024 + p1 < num_token)
                # Penalties: (presence_penalty, frequency_penalty, repetition_penalty)
                logits[seq_ids[pos2seq_id[vp]], token_ids[vp]] -= (
                    penalties[pos2seq_id[vp], 0] + token_cnt[vp] * penalties[pos2seq_id[vp], 1]
                )
                logits[seq_ids[pos2seq_id[vp]], token_ids[vp]] = T.if_then_else(
                    logits[seq_ids[pos2seq_id[vp]], token_ids[vp]] > 0,
                    logits[seq_ids[pos2seq_id[vp]], token_ids[vp]] * penalties[pos2seq_id[vp], 2],
                    logits[seq_ids[pos2seq_id[vp]], token_ids[vp]] / penalties[pos2seq_id[vp], 2],
                )


@T.prim_func
def _apply_bitmask_inplace(
    var_logits: T.handle,
    var_seq_ids: T.handle,
    var_bitmask: T.handle,
) -> None:
    """Function that applies vocabulary masking in place."""
    T.func_attr(
        {"global_symbol": "apply_bitmask_inplace", "tir.noalias": True, "tir.is_scheduled": True}
    )
    batch_size = T.int32(is_size_var=True)
    vocab_size = T.int32(is_size_var=True)
    num_seq = T.int32(is_size_var=True)
    logits = T.match_buffer(var_logits, (batch_size, vocab_size), "float32")
    seq_ids = T.match_buffer(var_seq_ids, (num_seq,), "int32")
    bitmask = T.match_buffer(var_bitmask, (batch_size, (vocab_size + 31) // 32), "int32")

    for fused_s_v_0 in T.thread_binding(0, (num_seq * vocab_size + 1023) // 1024, "blockIdx.x"):
        for fused_s_v_1 in T.thread_binding(0, 1024, "threadIdx.x"):
            with T.block("block"):
                vs = T.axis.spatial(num_seq, (fused_s_v_0 * 1024 + fused_s_v_1) // vocab_size)
                vv = T.axis.spatial(vocab_size, (fused_s_v_0 * 1024 + fused_s_v_1) % vocab_size)
                T.where(fused_s_v_0 * 1024 + fused_s_v_1 < num_seq * vocab_size)
                logits[seq_ids[vs], vv] = T.if_then_else(
                    (bitmask[seq_ids[vs], vv // 32] >> (vv % 32)) & 1 == 1,
                    logits[seq_ids[vs], vv],
                    T.float32(-1e10),
                )