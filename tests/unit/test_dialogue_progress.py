"""同一业务值换来源不是需求进展，来源仍保留在原Need。"""
from dataclasses import replace
from datetime import UTC, datetime

from domains.demand.schemas import NeedFieldView, ValidatedNeedView


def test_provenance_only_reconfirmation_does_not_reset_progress():
    from apps.scheduler_worker.dialogue_driver import semantic_need_hash
    need = ValidatedNeedView('need', 'account', 'Buyer', 'hinges',
        [NeedFieldView('quantity', '5000', 'msg1', 'We need 5000 pieces.')],
        2, ['destination'], 'validated', datetime.now(UTC), quantity=5000)
    repeated = replace(need, fields=[NeedFieldView('quantity', '5000', 'msg2', 'Yes, 5000 pieces.', 'employee')])
    assert semantic_need_hash(need) == semantic_need_hash(repeated)
    assert need.fields[0].source_ref != repeated.fields[0].source_ref
    changed = replace(repeated, quantity=6000, fields=[NeedFieldView('quantity', '6000', 'msg3')])
    assert semantic_need_hash(need) != semantic_need_hash(changed)
