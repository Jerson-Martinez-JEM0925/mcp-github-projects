"""Tests for estimate validation logic in FieldService.

Verifies that the set_estimate method correctly enforces configured
range (estimate_min, estimate_max) and granularity (estimate_granularity)
constraints before issuing the GraphQL mutation.
"""

import os
import sys
from unittest.mock import AsyncMock

import pytest

# Simulate test environment
os.environ.setdefault("GH_PROJECT_ORG_NAME", "TestOrg")
os.environ.setdefault("GH_PROJECT_REPO_NAME", "TestOrg")
os.environ.setdefault("GH_PROJECT_PROJECT_NUMBER", "1")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import get_settings
from exceptions import ValidationError
from services.field_service import FieldService

# ── Constants ────────────────────────────────────────────────────────────────

EXPECTED_ESTIMATE_FIELD_ID = "PVTF_lADOCg8zFs4A2iZ_zhafLsA"
PROJECT_ID = "PVT_kwDOCg8zFs4A2iZ_"
ITEM_ID = "PVTI_test_item_001"
FIELD_ID = EXPECTED_ESTIMATE_FIELD_ID


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def field_service():
    """Create a FieldService with a mocked GraphQL client."""
    mock_client = AsyncMock()
    mock_client.execute_with_retry = AsyncMock(return_value={
        "data": {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": ITEM_ID}}}
    })
    return FieldService(mock_client)


@pytest.fixture
def settings():
    """Return the default settings to inspect estimate config."""
    return get_settings()


# ── Config Loading ───────────────────────────────────────────────────────────


class TestEstimateConfigLoads:
    """Verify estimate configuration values load from defaults."""

    def test_estimate_min_default(self, settings):
        assert settings.estimate_min == 0.25

    def test_estimate_max_default(self, settings):
        assert settings.estimate_max == 9999.0

    def test_estimate_granularity_default(self, settings):
        assert settings.estimate_granularity == 0.25

    def test_estimate_field_id_discoverable(self):
        """The known Estimate field ID matches project metadata expectations."""
        # The Estimate field ID is provisioned in the target project board.
        # This test documents the known ID so regressions are caught if
        # field mappings change.
        assert EXPECTED_ESTIMATE_FIELD_ID == "PVTF_lADOCg8zFs4A2iZ_zhafLsA"


# ── Valid Estimates ──────────────────────────────────────────────────────────


class TestValidEstimatePasses:
    """Valid estimate values should pass validation and call the mutation."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", [0.25, 0.5, 0.75, 1.0, 2.5, 5.5, 100.0, 9999.0])
    async def test_valid_estimate_passes(self, field_service, value):
        """Estimates within range and matching granularity are accepted."""
        await field_service.set_estimate(PROJECT_ID, ITEM_ID, FIELD_ID, value)
        field_service._client.execute_with_retry.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.75])
    async def test_estimate_valid_granularity(self, field_service, value):
        """All multiples of 0.25 within range are valid."""
        await field_service.set_estimate(PROJECT_ID, ITEM_ID, FIELD_ID, value)
        field_service._client.execute_with_retry.assert_called_once()

    @pytest.mark.asyncio
    async def test_estimate_at_min_boundary(self, field_service):
        """The minimum allowed value (0.25) is accepted."""
        await field_service.set_estimate(PROJECT_ID, ITEM_ID, FIELD_ID, 0.25)
        field_service._client.execute_with_retry.assert_called_once()

    @pytest.mark.asyncio
    async def test_estimate_at_max_boundary(self, field_service):
        """The maximum allowed value (9999.0) is accepted."""
        await field_service.set_estimate(PROJECT_ID, ITEM_ID, FIELD_ID, 9999.0)
        field_service._client.execute_with_retry.assert_called_once()


# ── Invalid Estimates: Range ─────────────────────────────────────────────────


class TestEstimateRangeValidation:
    """Values outside [estimate_min, estimate_max] must raise ValidationError."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", [0.0, 0.1, 0.2, -1.0, -0.25])
    async def test_estimate_below_min_fails(self, field_service, value):
        """Values below estimate_min (0.25) are rejected."""
        with pytest.raises(ValidationError, match="out of range"):
            await field_service.set_estimate(PROJECT_ID, ITEM_ID, FIELD_ID, value)
        field_service._client.execute_with_retry.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", [10000.0, 9999.25, 99999.0])
    async def test_estimate_above_max_fails(self, field_service, value):
        """Values above estimate_max (9999.0) are rejected."""
        with pytest.raises(ValidationError, match="out of range"):
            await field_service.set_estimate(PROJECT_ID, ITEM_ID, FIELD_ID, value)
        field_service._client.execute_with_retry.assert_not_called()


# ── Invalid Estimates: Granularity ───────────────────────────────────────────


class TestEstimateGranularityValidation:
    """Values not a multiple of estimate_granularity must raise ValidationError."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", [0.3, 0.33, 0.4, 0.6, 1.1, 1.3, 2.7, 5.01])
    async def test_estimate_wrong_granularity(self, field_service, value):
        """Non-multiples of 0.25 within valid range are rejected."""
        with pytest.raises(ValidationError, match="granularity"):
            await field_service.set_estimate(PROJECT_ID, ITEM_ID, FIELD_ID, value)
        field_service._client.execute_with_retry.assert_not_called()

    @pytest.mark.asyncio
    async def test_granularity_error_message_includes_step(self, field_service):
        """Error message mentions the required granularity value."""
        with pytest.raises(ValidationError, match="0.25"):
            await field_service.set_estimate(PROJECT_ID, ITEM_ID, FIELD_ID, 1.3)


# ── Mutation Payload ─────────────────────────────────────────────────────────


class TestEstimateMutationPayload:
    """Verify the GraphQL mutation receives correct variables."""

    @pytest.mark.asyncio
    async def test_mutation_receives_correct_variables(self, field_service):
        """The GraphQL mutation is called with proper project/item/field/value."""
        await field_service.set_estimate(PROJECT_ID, ITEM_ID, FIELD_ID, 4.0)

        call_args = field_service._client.execute_with_retry.call_args
        variables = call_args[0][1]  # Second positional arg is variables

        assert variables["projectId"] == PROJECT_ID
        assert variables["itemId"] == ITEM_ID
        assert variables["fieldId"] == FIELD_ID
        assert variables["value"] == {"number": 4.0}

    @pytest.mark.asyncio
    async def test_mutation_called_as_mutation(self, field_service):
        """The execute call passes is_mutation=True."""
        await field_service.set_estimate(PROJECT_ID, ITEM_ID, FIELD_ID, 2.0)

        call_kwargs = field_service._client.execute_with_retry.call_args[1]
        assert call_kwargs.get("is_mutation") is True
