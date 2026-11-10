"""The compaction summary had no size cap, and the anti-thrash breaker could mask an overflow.

Two defects, one causal chain (measured on the backend fleet, 2026-11-10; card t_1ea74f94,
crash of t_5a6c1faf):

1. ``max_summary_tokens`` was only ever PROMPT GUIDANCE. ``_compute_summary_budget``
   computes the budget and passes it into the summarizer prompt; nothing bounded the
   text that came BACK. An iterative update asks the model to "PRESERVE all existing
   information" and feeds it the previous summary, so the model can quote it. Measured:
   90.3% of the fleet's stored summaries exceeded the 40,000-char (10,000-token)
   ceiling; the worst was 432,166 chars (108,041 tokens).

   That matters because the summary IS the live context. The crash session's live
   context was 165,620 tokens against a 153,600-token trigger, and 106,004 of those
   tokens were ONE summary. The compaction could not clear its own threshold, so every
   pass scored "ineffective".

2. Two "ineffective" strikes trip the anti-thrash breaker, which then suppresses
   auto-compaction for ``_ANTI_THRASH_RECOVERY_SECONDS`` (300s). While it suppresses,
   nothing bounds the request. Measured: all 6 sessions with
   ``compression_ineffective_count >= 2`` ended ``ws_orphan_reap``, 129.8s-267.2s after
   the breaker armed - every one died before the 300s probation probe could fire.

The contract pinned here:

* The ASSEMBLED summary is bounded by ``max_summary_tokens`` on BOTH the LLM path and
  the deterministic-fallback path. The bound keeps head and tail; the tail is favoured
  because it carries the most recent state.
* The bound is applied AFTER the handoff preamble is prepended and AFTER the lean
  augmentation appends its sections, so nothing escapes the cap.
* The anti-thrash breaker still blocks normally - it is a cost guard.
* It stops blocking once the request is already past the provider's context window:
  past that point there is nothing left to protect, and riding on is a certain
  overflow.
"""

from unittest.mock import patch

from agent.context_compressor import ContextCompressor, SUMMARY_PREFIX
from agent.model_metadata import CHARS_PER_TOKEN


def _compressor(context_length: int = 204_800, threshold_percent: float = 0.75) -> ContextCompressor:
    cc = ContextCompressor(
        model="MiniMax-M2.7-highspeed",
        threshold_percent=threshold_percent,
        protect_first_n=3,
        protect_last_n=20,
        quiet_mode=True,
        config_context_length=context_length,
        provider="test",
    )
    # Durable state is not under test here; keep the compressor in-memory.
    cc._durable_read = lambda *a, **k: (False, None)
    cc._durable_write = lambda *a, **k: True
    return cc


class TestSummarySizeCap:
    """The declared ceiling must be enforced on the assembled artifact."""

    def test_oversized_summary_is_capped(self):
        cc = _compressor()
        ceiling = cc.max_summary_tokens * CHARS_PER_TOKEN
        # The shape measured on the crash session: a summary far above the ceiling.
        huge = "A" * (ceiling * 4)
        out = cc._bound_summary_output(huge, ceiling)
        assert len(out) <= ceiling, f"summary still {len(out)} chars > {ceiling}"

    def test_cap_keeps_head_and_tail(self):
        cc = _compressor()
        ceiling = 1000
        out = cc._bound_summary_output("HEAD" + ("x" * 5000) + "TAIL", ceiling)
        assert "HEAD" in out
        assert "TAIL" in out
        assert "[summary truncated by the size guard]" in out

    def test_cap_favours_the_tail(self):
        """The tail carries the most recent state; it must survive the cut."""
        cc = _compressor()
        ceiling = 1000
        out = cc._bound_summary_output("HEAD" + ("x" * 5000) + "TAIL", ceiling)
        # The tail keeps 55% of the kept budget, the head 45%.
        assert out.index("TAIL") > out.index("HEAD")
        assert len(out) - len(out[out.index("TAIL"):]) > (len(out) * 0.45)

    def test_summary_under_the_cap_is_untouched(self):
        cc = _compressor()
        text = "small summary"
        assert cc._bound_summary_output(text, 100_000) == text

    def test_zero_cap_is_a_no_op(self):
        """A zero/absent budget must not truncate to nothing."""
        cc = _compressor()
        text = "summary"
        assert cc._bound_summary_output(text, 0) == text

    def test_llm_path_caps_the_returned_handoff(self):
        """_generate_summary must return a bounded handoff, not just cap a helper."""
        cc = _compressor()
        ceiling = cc.max_summary_tokens * CHARS_PER_TOKEN
        # A summarizer that ignores the budget and quotes a huge previous summary.
        huge = "## Active Task\n" + ("prior content " * 200_000)
        with patch.object(cc, "_call_summary_llm", return_value=huge), \
             patch.object(cc, "_validate_summary_user_provenance", return_value=None), \
             patch("agent.conversation_compression._raise_if_stale_attempt", return_value=None):
            out = cc._generate_summary([{"role": "user", "content": "hello"}])
        assert out is not None
        assert len(out) <= ceiling, f"handoff {len(out)} chars > ceiling {ceiling}"
        assert out.startswith(SUMMARY_PREFIX)

    def test_fallback_path_caps_the_returned_handoff(self):
        """The deterministic fallback appends lean sections AFTER its own 8K cap."""
        cc = _compressor()
        ceiling = cc.max_summary_tokens * CHARS_PER_TOKEN
        turns = [
            {"role": "user", "content": "x" * 20_000},
            {"role": "assistant", "content": "y" * 20_000},
            {"role": "tool", "content": "z" * 20_000, "tool_call_id": "t1"},
        ]
        out = cc._build_static_fallback_summary(turns, reason="summary model down")
        assert out is not None
        assert len(out) <= ceiling, f"fallback handoff {len(out)} chars > ceiling {ceiling}"


class TestAntiThrashOverflowEscape:
    """The breaker is a cost guard; it must not become the cause of an overflow."""

    def _tripped(self, cc: ContextCompressor) -> ContextCompressor:
        cc._record_ineffective_compression_verdict(2)
        return cc

    def test_breaker_blocks_a_normal_over_threshold_request(self):
        cc = self._tripped(_compressor())
        cc.last_real_prompt_tokens = 160_000  # over the trigger, under the window
        should, reason = cc.should_compress_info(160_000)
        assert should is False
        assert reason == "ineffective"

    def test_breaker_does_not_block_past_the_provider_window(self):
        """Past the provider's window the session is already headed for an overflow."""
        cc = self._tripped(_compressor(context_length=204_800))
        cc.last_real_prompt_tokens = 300_000  # past the 204,800 window
        should, reason = cc.should_compress_info(300_000)
        assert should is True, "the breaker must not suppress compaction past the window"
        assert reason is None

    def test_escape_hatch_is_closed_under_the_window(self):
        cc = _compressor()
        cc.last_real_prompt_tokens = 204_799
        assert cc._request_past_provider_window() is False
        cc.last_real_prompt_tokens = 204_801
        assert cc._request_past_provider_window() is True

    def test_escape_hatch_is_closed_without_a_known_window(self):
        cc = _compressor()
        cc.context_length = 0
        cc.last_real_prompt_tokens = 10_000_000
        assert cc._request_past_provider_window() is False

    def test_escape_hatch_falls_back_to_last_prompt_tokens(self):
        cc = _compressor()
        cc.last_real_prompt_tokens = 0
        cc.last_prompt_tokens = 300_000
        assert cc._request_past_provider_window() is True


class TestRegressionCrashShape:
    """The measured crash session, replayed through the fixed compressor."""

    def test_crash_session_summary_now_fits_the_budget(self):
        """The real 424,018-char artifact, capped, leaves room for the session to live."""
        cc = _compressor()
        ceiling = cc.max_summary_tokens * CHARS_PER_TOKEN
        # The real stored artifact shape (prefix + quoted previous summary + body).
        real_artifact = (
            SUMMARY_PREFIX
            + "## Historical Task Snapshot\n" + ("prior summary text " * 30_000)
            + "## Active State\nstill running\n"
        )
        assert len(real_artifact) > ceiling, "the fixture must exceed the cap"
        capped = cc._bound_summary_output(real_artifact, ceiling)
        assert len(capped) <= ceiling
        # The live context of the crash session was 165,620 tokens with a
        # 59,616-token remainder; a capped summary must leave the total under
        # the 153,600-token trigger so a compaction can clear it.
        remainder_tokens = 59_616
        total = len(capped) // CHARS_PER_TOKEN + remainder_tokens
        assert total < 153_600, f"live context {total} still over the trigger"
