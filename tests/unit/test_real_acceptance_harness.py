from scripts.real_acceptance import conversation_metrics


def test_conversation_metrics_flags_four_question_turns_and_prompt_leak():
    metrics = conversation_metrics(
        [
            {"reply": "本当に？"},
            {"reply": "それから？"},
            {"reply": "なぜ？"},
            {"reply": "どうして？"},
            {"reply": "```json\n{\"answer\": true}"},
        ]
    )

    assert metrics["max_consecutive_question_turns"] == 4
    assert metrics["leak_matches"]
    assert metrics["automatic_hard_fail"] is True


def test_conversation_metrics_flags_trailing_structured_residue():
    metrics = conversation_metrics([{"reply": "静かになるかもしれませんね。”}"}])

    assert metrics["leak_matches"]
    assert metrics["automatic_hard_fail"] is True


def test_conversation_metrics_flags_replayed_prior_user_message():
    metrics = conversation_metrics(
        [
            {"prompt": "私はそうは言っていない", "reply": "わかりました"},
            {"prompt": "覚えてる？", "reply": "私はそうは言っていない"},
        ]
    )

    assert metrics["replayed_user_messages"] == [
        {"reply_turn": 2, "source_user_turn": 1}
    ]
    assert metrics["automatic_hard_fail"] is True


def test_conversation_metrics_does_not_silently_pass_suppression():
    metrics = conversation_metrics(
        [{"prompt": "何歳？", "reply": None, "suppressed": True}]
    )

    assert metrics["suppressed"] == 1
    assert metrics["automatic_hard_fail"] is True


def test_conversation_metrics_accepts_varied_non_question_replies():
    metrics = conversation_metrics(
        [
            {"reply": "はじめまして。ゆいです。"},
            {"reply": "今日は静かに過ごしているよ。"},
            {"reply": "そうなんだね。"},
        ]
    )

    assert metrics["max_consecutive_question_turns"] == 0
    assert metrics["duplicate_replies"] == []
    assert metrics["leak_matches"] == []
    assert metrics["automatic_hard_fail"] is False
