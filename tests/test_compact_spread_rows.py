"""Parsed rows stay compact while preserving their full public contract."""
import copy
import json
import pickle
import sys
import weakref
from dataclasses import FrozenInstanceError, fields, replace

import pytest
from test_tokenized_lane_and_executor_boundary import _row


def test_row_container_footprint_fits_per_route_budget():
    row = _row()
    owned = sys.getsizeof(row) + sys.getsizeof(getattr(row, '__dict__', {}))
    assert owned <= 1024, f'{owned} bytes of containers per parsed route'


def test_public_mapping_preserves_all_fields_defaults_and_shallow_values():
    row = _row(blockers=['identity_pending'], dex_route_plan=('route-a',))
    payload = row.to_dict()
    assert list(payload) == [field.name for field in fields(row)]
    assert payload['blockers'] is row.blockers
    assert payload['dex_route_plan'] is row.dex_route_plan
    assert payload['long_funding_interval_assumed'] is False
    assert payload['asset_class'] == 'crypto'
    assert payload['long_index_price'] is None
    assert json.loads(json.dumps(payload))['blockers'] == ['identity_pending']
    payload['token'] = 'OTHER'
    assert row.token == 'SIREN'


def test_replace_copy_and_pickle_preserve_frozen_row_contract():
    row = _row()
    changed = replace(row, token='GPRO', funding_daily_pct=0.0)
    assert changed.token == 'GPRO' and row.token == 'SIREN'
    assert changed.blockers is row.blockers
    assert changed.funding_daily_pct == 0.0
    for restored in (copy.copy(row), pickle.loads(pickle.dumps(row))):
        assert restored.to_dict() == row.to_dict()
    with pytest.raises(FrozenInstanceError):
        row.token = 'OTHER'


def test_rows_remain_weak_referenceable():
    row = _row()
    assert weakref.ref(row)() is row
