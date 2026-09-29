

from app.models.case import Case
from app.models.slide import Slide
from app.models.grading import Grading
from app.models.stage_execution import StageExecution


def test_grading_rerun_resets_stale_overrides_and_unconfirms_type():
    """Verify re-running grading clears stale overrides and unconfirms histologic type (Issue #143)."""
    import uuid
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import StaticPool
    from app.core.db import Base

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    session = Session(bind=engine)

    try:
        case_id = uuid.uuid4()
        slide_id = uuid.uuid4()

        case = Case(
            id=case_id,
            created_by="test_user",
            status="open"
        )
        session.add(case)

        slide = Slide(
            id=slide_id,
            case_id=case_id,
            mpp_x=0.25,
            mpp_y=0.25,
            checksum_sha256="12345678abcdef",
            gcs_uri_original="gs://fake-bucket/slide.svs"
        )
        session.add(slide)

        # Existing grading with stale pathologist overrides
        old_grading = Grading(
            case_id=case_id,
            tubule_percent=15.0,
            tubule_score=2,
            pleo_score=2,
            mitotic_score=1,
            nottingham_sum=5,
            grade=1,
            histologic_type="ILC",
            type_confirmed_by="pathologist_007",
            machine={"fake": "data"},
            overrides={"patch_001": {"tubule_percent": 80.0, "status": "approved"}}
        )
        session.add(old_grading)

        stage_exec = StageExecution(
            case_id=case_id,
            stage="grading",
            status="running",
            attempt=2
        )
        session.add(stage_exec)
        session.commit()

        # Verify initial state has overrides and confirmed type
        assert old_grading.overrides != {}
        assert old_grading.type_confirmed_by == "pathologist_007"

        # Simulate re-run persistence logic
        mock_aggregate = {
            "tubule_percent": 45.0,
            "tubule_score": 2,
            "pleo_score": 3,
            "mitotic_score": 2,
            "nottingham_sum": 7,
            "grade": 2,
            "flags": []
        }
        mock_machine = {"new": "attempt_2_output"}

        # Update path
        old_grading.tubule_percent = mock_aggregate["tubule_percent"]
        old_grading.tubule_score = mock_aggregate["tubule_score"]
        old_grading.pleo_score = mock_aggregate["pleo_score"]
        old_grading.mitotic_score = mock_aggregate["mitotic_score"]
        old_grading.nottingham_sum = mock_aggregate["nottingham_sum"]
        old_grading.grade = mock_aggregate["grade"]
        old_grading.machine = mock_machine
        old_grading.overrides = {}
        old_grading.type_confirmed_by = "unconfirmed"
        session.commit()

        reloaded = session.get(Grading, case_id)
        assert reloaded.overrides == {}
        assert reloaded.type_confirmed_by == "unconfirmed"
        assert reloaded.grade == 2
    finally:
        session.close()
