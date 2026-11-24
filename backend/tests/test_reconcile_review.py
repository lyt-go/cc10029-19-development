"""到达对账复核链路测试：暂存分区版本、可复核清单、清单驱动对账与历史复算。

覆盖题目场景：
- 同一批次两个暂存分区，一个确认 4 行，另一个正在补第 5 行；
- 操作员提交筛选、生成清单后才执行对账，第二个分区此时可能确认；
- 一份对账必须重现选中的批次、分区与数据版本，不能合读两个版本；
- 保留两份确认结果与实际分区的关联，历史列表可分别打开 4 行与 5 行版本。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.services.arrival_reconcile import ArrivalReconcileService
from app.services.staging_area import StagingAreaService
from app.store import store


@pytest.fixture(autouse=True)
def clean_runtime_data():
    """隔离 store 数据，避免用例之间互相干扰。"""
    from app.seed import SEED_ROWS

    store._tables = {
        name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
    }
    yield


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    """把落盘根目录整体指到临时目录，测试不污染仓库 data/。"""
    root = tmp_path / "data"

    def _dir(self, sub: str) -> Path:
        path = root / sub
        path.mkdir(parents=True, exist_ok=True)
        return path

    monkeypatch.setattr(StagingAreaService, "_data_dir", _dir)
    monkeypatch.setattr(ArrivalReconcileService, "_data_dir", _dir)
    return root


@pytest.fixture
def staging(data_root):
    return StagingAreaService()


@pytest.fixture
def reconcile(data_root):
    return ArrivalReconcileService()


def _land_two_partitions(staging: StagingAreaService):
    """落两个分区：P1 4 行全到，P2 应到 5 行但此刻只落地 4 行。"""
    p1 = staging.land_batch("TRAN-REVIEW", 4, partition_no=1,
                            start_index=0, expected_count=4)
    staging.confirm_land(p1["staging_id"])
    p2 = staging.land_batch("TRAN-REVIEW", 4, partition_no=2,
                            start_index=4, expected_count=5)
    staging.confirm_land(p2["staging_id"])
    return p1, p2


class TestStagingPartitionVersions:
    def test_partition_query_lists_all_batch_partitions(self, staging):
        _land_two_partitions(staging)
        partitions = staging.list_batch_partitions("TRAN-REVIEW")
        assert [p["staging_code"] for p in partitions] == [
            "STAG-TRAN-REVIEW-P1", "STAG-TRAN-REVIEW-P2"]
        assert [p["partition_no"] for p in partitions] == ["1", "2"]
        assert all(p["latest_confirmed_version"] for p in partitions)

    def test_confirm_freezes_immutable_version(self, staging):
        p1 = staging.land_batch("TRAN-REVIEW", 4, partition_no=1,
                                start_index=0, expected_count=4)
        staging.confirm_land(p1["staging_id"])
        v1 = staging.read_version("STAG-TRAN-REVIEW-P1", 1)
        assert v1 is not None and len(v1["rows"]) == 4 and v1["confirmed"]

        # 补录只追加新版本，历史版本文件内容与校验值不变
        staging.append_rows(p1["staging_id"], count=1, confirm=True)
        again = staging.read_version("STAG-TRAN-REVIEW-P1", 1)
        assert len(again["rows"]) == 4
        assert again["checksum"] == v1["checksum"]
        assert len(staging.read_version("STAG-TRAN-REVIEW-P1", 2)["rows"]) == 5

    def test_backfill_keeps_global_record_keys(self, staging):
        _, p2 = _land_two_partitions(staging)
        staging.append_rows(p2["staging_id"], count=1, confirm=True)
        keys = [r["record_key"] for r in
                staging.read_version("STAG-TRAN-REVIEW-P2", 2)["rows"]]
        assert keys == [f"TRAN-REVIEW:{n}" for n in range(5, 10)]


class TestReviewManifest:
    def test_manifest_pins_version_and_expected_rows(self, staging, reconcile):
        _land_two_partitions(staging)
        manifest = reconcile.build_review_manifest("TRAN-REVIEW")
        assert manifest["partition_count"] == 2
        assert manifest["expected_total"] == 9  # 4 + 5，缺失的第 5 行计入应到
        pinned = {p["staging_code"]: p for p in manifest["partitions"]}
        assert pinned["STAG-TRAN-REVIEW-P1"]["version"] == 1
        assert pinned["STAG-TRAN-REVIEW-P1"]["record_count"] == 4
        assert pinned["STAG-TRAN-REVIEW-P2"]["record_count"] == 4

    def test_manifest_is_stable_after_backfill(self, staging, reconcile):
        _, p2 = _land_two_partitions(staging)
        manifest = reconcile.build_review_manifest("TRAN-REVIEW")
        checksum = manifest["manifest_checksum"]
        staging.append_rows(p2["staging_id"], count=1, confirm=True)
        reloaded = reconcile.get_manifest(manifest["manifest_code"])
        assert reloaded["manifest_checksum"] == checksum
        assert [p["version"] for p in reloaded["partitions"]] == [1, 1]


class TestManifestDrivenReconciliation:
    def test_reconcile_reads_only_manifest_versions(self, staging, reconcile):
        _land_two_partitions(staging)
        manifest = reconcile.build_review_manifest("TRAN-REVIEW")
        result = reconcile.execute_from_manifest(manifest["manifest_code"])
        assert result["expected"] == 9
        assert result["actual"] == 8  # 清单钉住的是 4 + 4
        assert result["missing"] == 1
        assert result["missing_row_keys"] == ["TRAN-REVIEW:9"]
        assert result["used_alternate_version"] is False

    def test_late_confirmation_switches_version_and_records_it(self, staging, reconcile):
        _, p2 = _land_two_partitions(staging)
        manifest = reconcile.build_review_manifest("TRAN-REVIEW")
        # 清单生成后，第二个分区补到第 5 行并再次确认
        staging.append_rows(p2["staging_id"], count=1, confirm=True)

        result = reconcile.execute_from_manifest(manifest["manifest_code"])
        assert result["actual"] == 9
        assert result["missing"] == 0
        assert result["balanced"] is True
        assert result["used_alternate_version"] is True
        assert result["alternate_details"] == [{
            "staging_code": "STAG-TRAN-REVIEW-P2",
            "partition_no": "2",
            "pinned_version": 1,
            "used_version": 2,
            "reason": "筛选后该分区又确认了新版本，改用最新确认版本",
        }]
        # 每个分区只读到一个版本，不会合读
        by_code = {p["staging_code"]: p for p in result["partitions"]}
        assert by_code["STAG-TRAN-REVIEW-P1"]["data_version"] == "v1"
        assert by_code["STAG-TRAN-REVIEW-P2"]["data_version"] == "v2"
        assert by_code["STAG-TRAN-REVIEW-P2"]["actual_count"] == 5

    def test_two_confirmations_stay_attached_to_partitions(self, staging, reconcile):
        _, p2 = _land_two_partitions(staging)
        manifest = reconcile.build_review_manifest("TRAN-REVIEW")
        staging.append_rows(p2["staging_id"], count=1, confirm=True)
        result = reconcile.execute_from_manifest(manifest["manifest_code"])
        by_code = {p["staging_code"]: p for p in result["partitions"]}

        p1_confirmations = by_code["STAG-TRAN-REVIEW-P1"]["confirmation"]["confirmations"]
        assert [(c["confirmed_version"], c["record_count"]) for c in p1_confirmations] == [(1, 4)]
        p2_confirmations = by_code["STAG-TRAN-REVIEW-P2"]["confirmation"]["confirmations"]
        assert [(c["confirmed_version"], c["record_count"]) for c in p2_confirmations] == [
            (1, 4), (2, 5)]

    def test_history_keeps_both_openable_versions(self, staging, reconcile):
        _, p2 = _land_two_partitions(staging)
        manifest = reconcile.build_review_manifest("TRAN-REVIEW")
        staging.append_rows(p2["staging_id"], count=1, confirm=True)
        result = reconcile.execute_from_manifest(manifest["manifest_code"])
        p2_result = next(p for p in result["partitions"]
                         if p["staging_code"] == "STAG-TRAN-REVIEW-P2")
        versions = {v["version"]: v for v in p2_result["available_versions"]}
        assert versions[1]["record_count"] == 4
        assert versions[2]["record_count"] == 5

        # 历史列表按留存版本分别打开 4 行与 5 行两份
        assert len(staging.read_version("STAG-TRAN-REVIEW-P2", 1)["rows"]) == 4
        assert len(staging.read_version("STAG-TRAN-REVIEW-P2", 2)["rows"]) == 5
        snapshot = reconcile.get_reconciliation_snapshot(result["recon_id"])
        assert snapshot["used_alternate_version"] is True
        assert snapshot["manifest_code"] == manifest["manifest_code"]


class TestHistoryRecompute:
    def test_recompute_reproduces_snapshot_after_more_backfill(self, staging, reconcile):
        _, p2 = _land_two_partitions(staging)
        manifest = reconcile.build_review_manifest("TRAN-REVIEW")
        staging.append_rows(p2["staging_id"], count=1, confirm=True)
        result = reconcile.execute_from_manifest(manifest["manifest_code"])

        # 对账之后分区继续补录并确认，仍不能影响已留存的复算结果
        staging.append_rows(p2["staging_id"], count=1, confirm=True)
        recomputed = reconcile.recompute_reconciliation(result["recon_id"])
        assert recomputed["matches_snapshot"] is True
        assert recomputed["expected"] == 9
        assert recomputed["actual"] == 9
        assert recomputed["missing"] == 0
        assert recomputed["used_alternate_version"] is True
        by_code = {p["staging_code"]: p for p in recomputed["partitions"]}
        assert by_code["STAG-TRAN-REVIEW-P2"]["data_version"] == "v2"
        assert len(staging.read_version("STAG-TRAN-REVIEW-P2", 2)["rows"]) == 5
