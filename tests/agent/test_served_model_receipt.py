"""Provider-attested served model, recorded per API call.

Nothing previously carried the provider-reported model out of the conversation
loop: the only read of ``response.model`` sat behind a ``has_hook`` guard, so
every source-card delegation row recorded the CONFIGURED model echoed back.
A silent provider substitution was therefore invisible to the gateway.

The receipt is a LIST, not a scalar: live rows show 16-20 API calls in one
turn, and the existing per-turn stash keeps only the last call by design.
"""

from types import SimpleNamespace

import pytest

from agent.served_model import (
    record_served_model,
    reset_served_models,
    served_model_receipt,
    served_model_mismatches,
)


def _agent():
    return SimpleNamespace()


def test_receipt_is_a_list_not_a_scalar():
    agent = _agent()
    reset_served_models(agent)
    record_served_model(agent, requested="glm-5.3", response=SimpleNamespace(model="glm-5.3"))
    record_served_model(agent, requested="glm-5.3", response=SimpleNamespace(model="glm-5.3"))
    record_served_model(agent, requested="glm-5.3", response=SimpleNamespace(model="glm-5.4"))

    receipt = served_model_receipt(agent)
    assert [row["served"] for row in receipt] == ["glm-5.3", "glm-5.3", "glm-5.4"]
    assert [row["requested"] for row in receipt] == ["glm-5.3"] * 3
    assert [row["call"] for row in receipt] == [1, 2, 3]


def test_reset_clears_the_accumulator_between_turns():
    agent = _agent()
    reset_served_models(agent)
    record_served_model(agent, requested="glm-5.3", response=SimpleNamespace(model="glm-5.3"))
    reset_served_models(agent)
    assert served_model_receipt(agent) == []


def test_mismatch_is_evaluated_against_the_route_as_of_that_call():
    """`agent.model` is legitimately reassigned on a provider switch.

    A turn-level scalar comparison would therefore flag every legitimate
    fallback. Each row carries the model requested for THAT call.
    """
    agent = _agent()
    reset_served_models(agent)
    record_served_model(agent, requested="glm-5.3", response=SimpleNamespace(model="glm-5.3"))
    # legitimate switch: the route itself changed, and the provider honoured it
    record_served_model(
        agent, requested="deepseek-v4-pro", response=SimpleNamespace(model="deepseek-v4-pro")
    )
    assert served_model_mismatches(agent) == []


def test_substitution_is_reported_as_a_mismatch():
    agent = _agent()
    reset_served_models(agent)
    record_served_model(agent, requested="glm-5.2", response=SimpleNamespace(model="glm-5.3"))
    mismatches = served_model_mismatches(agent)
    assert len(mismatches) == 1
    assert mismatches[0]["requested"] == "glm-5.2"
    assert mismatches[0]["served"] == "glm-5.3"


def test_absent_response_model_is_recorded_as_unattested():
    """A provider that reports nothing must not read as a clean match."""
    agent = _agent()
    reset_served_models(agent)
    record_served_model(agent, requested="glm-5.3", response=SimpleNamespace())
    receipt = served_model_receipt(agent)
    assert receipt[0]["served"] is None
    assert served_model_mismatches(agent)[0]["served"] is None


@pytest.mark.parametrize(
    "served, requested",
    [
        ("zai/glm-5.3", "glm-5.3"),
        ("glm-5.3", "zai/glm-5.3"),
        ("GLM-5.3", "glm-5.3"),
        ("glm-5.3 ", " glm-5.3"),
    ],
)
def test_provider_qualified_and_cased_names_are_not_false_mismatches(served, requested):
    agent = _agent()
    reset_served_models(agent)
    record_served_model(agent, requested=requested, response=SimpleNamespace(model=served))
    assert served_model_mismatches(agent) == []


def test_recording_never_raises_on_a_hostile_response_object():
    class Hostile:
        @property
        def model(self):
            raise RuntimeError("provider client blew up")

    agent = _agent()
    reset_served_models(agent)
    record_served_model(agent, requested="glm-5.3", response=Hostile())
    assert served_model_receipt(agent)[0]["served"] is None
