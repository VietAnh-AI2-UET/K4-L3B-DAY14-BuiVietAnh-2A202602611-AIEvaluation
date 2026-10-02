"""Verify Gemini generation and compatibility with the evaluation pipeline."""

import json
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from google.genai import errors, types
from httpx import ConnectError

import domain_assistant as assistant_module
import gemini_rate_limit as rate_limit_module
from evaluate_answers import load_evaluation_inputs


PROJECT_DIR = Path(__file__).resolve().parent.parent
CORPUS_DIR = PROJECT_DIR / "data" / "technology_store"
DATASET_PATH = PROJECT_DIR / "golden_dataset.json"


@pytest.fixture
def gemini_client(monkeypatch, tmp_path):
    for name in (
        "GOOGLE_API_KEY", "GEMINI_API_KEY", "GEMINI_MODEL", "GEMINI_RPM", "GEMINI_RPD"
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        rate_limit_module, "DEFAULT_STATE_PATH", tmp_path / "usage.sqlite3"
    )
    client = Mock()
    factory = Mock(return_value=client)
    monkeypatch.setattr(assistant_module.genai, "Client", factory)
    return client, factory


@pytest.mark.parametrize(
    ("google_key", "gemini_key", "expected_key"),
    [
        (" google-test-key ", "", "google-test-key"),
        ("", " gemini-test-key ", "gemini-test-key"),
        ("   ", "gemini-test-key", "gemini-test-key"),
        ("google-test-key", "gemini-test-key", "google-test-key"),
    ],
)
def test_accepts_gemini_keys(monkeypatch, gemini_client, google_key, gemini_key, expected_key):
    _, factory = gemini_client
    monkeypatch.setenv("GOOGLE_API_KEY", google_key)
    monkeypatch.setenv("GEMINI_API_KEY", gemini_key)

    assistant_module.GeminiGenerator()

    factory.assert_called_once()
    assert factory.call_args.kwargs["api_key"] == expected_key
    assert factory.call_args.kwargs["http_options"].retry_options.attempts == 1


def test_requires_gemini_key_even_when_openai_key_exists(monkeypatch, gemini_client):
    _, factory = gemini_client
    monkeypatch.setenv("OPENAI_API_KEY", "unused-openai-test-key")

    with pytest.raises(RuntimeError, match="GOOGLE_API_KEY"):
        assistant_module.GeminiGenerator()

    factory.assert_not_called()


@pytest.mark.parametrize("model", [None, "", "   "])
def test_defaults_model_when_unset_or_blank(monkeypatch, gemini_client, model):
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    if model is not None:
        monkeypatch.setenv("GEMINI_MODEL", model)

    generator = assistant_module.GeminiGenerator()

    assert generator.model == "gemini-3.5-flash-lite"


def test_generates_text_with_configured_model(monkeypatch, gemini_client):
    client, _ = gemini_client
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_MODEL", " gemini-3.8-flash ")
    client.models.generate_content.return_value = types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(parts=[types.Part(text="  Generated answer.\n")])
            )
        ]
    )
    generator = assistant_module.GeminiGenerator(max_output_tokens=2048)

    answer = generator.generate("Question and retrieved context")

    assert answer == "Generated answer."
    request = client.models.generate_content.call_args.kwargs
    assert request["model"] == "gemini-3.8-flash"
    assert request["contents"] == "Question and retrieved context"
    assert request["config"].max_output_tokens == 2048


@pytest.mark.parametrize("text", [None, "", "   "])
def test_rejects_empty_or_blocked_responses(monkeypatch, gemini_client, text):
    client, _ = gemini_client
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    client.models.generate_content.return_value = SimpleNamespace(text=text)
    generator = assistant_module.GeminiGenerator()

    with pytest.raises(RuntimeError, match="Gemini returned an empty answer"):
        generator.generate("Question")


def test_default_assistant_uses_gemini(monkeypatch, gemini_client):
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")

    assistant = assistant_module.DomainAssistant.from_corpus(CORPUS_DIR)

    assert isinstance(assistant.generator, assistant_module.GeminiGenerator)


def test_failed_request_still_consumes_budget(monkeypatch, gemini_client):
    client, _ = gemini_client
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    client.models.generate_content.side_effect = errors.ServerError(
        503, {"error": {"message": "Temporarily unavailable"}}
    )
    generator = assistant_module.GeminiGenerator()

    with pytest.raises(errors.ServerError):
        generator.generate("Question")

    assert generator.rate_limiter.ensure_capacity(499) == 499
    client.models.generate_content.assert_called_once()


def test_batch_checks_daily_budget_before_sending(monkeypatch, gemini_client):
    client, _ = gemini_client
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_RPD", "19")

    with pytest.raises(RuntimeError, match="this run needs 20"):
        assistant_module.generate_actual_answers(DATASET_PATH, CORPUS_DIR)

    client.models.generate_content.assert_not_called()


def test_generated_answers_remain_compatible_with_evaluation(tmp_path, gemini_client):
    generator = Mock()
    generator.model = "test-gemini-model"
    generator.generate.return_value = "Answer based on the retrieved context."

    artifact = assistant_module.generate_actual_answers(
        DATASET_PATH, CORPUS_DIR, generator=generator
    )
    actual_path = tmp_path / "actual_answers.json"
    actual_path.write_text(json.dumps(artifact), encoding="utf-8")
    qa_pairs, answers = load_evaluation_inputs(DATASET_PATH, actual_path)

    assert len(artifact["answers"]) == 20
    assert len(qa_pairs) == len(answers) == 20
    assert artifact["agent"]["model"] == "test-gemini-model"
    assert all(record["error"] is None for record in artifact["answers"])
    assert all(record["retrieved_contexts"] for record in artifact["answers"])


@pytest.mark.parametrize(
    "failure",
    [
        errors.ClientError(429, {"error": {"message": "Quota exceeded"}}),
        ConnectError("Unable to connect to Gemini"),
    ],
)
def test_cli_reports_gemini_errors_without_writing_artifact(
    monkeypatch, tmp_path, failure, capsys
):
    output = tmp_path / "actual_answers.json"
    monkeypatch.setattr(
        assistant_module,
        "parse_args",
        lambda: Namespace(
            dataset=DATASET_PATH, corpus_dir=CORPUS_DIR, output=output, top_k=5
        ),
    )
    monkeypatch.setattr(
        assistant_module, "generate_actual_answers", Mock(side_effect=failure)
    )

    assert assistant_module.main() == 2
    assert "ERROR:" in capsys.readouterr().out
    assert not output.exists()
