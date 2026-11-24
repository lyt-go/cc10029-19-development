"""到达对账领域服务：维护批次应到与实到记录数的对账、迟到与缺失条数统计及结论。

复核链路的三个约束都由本模块负责：

1. 清单先行：``build_review_manifest`` 查询批次对应的暂存分区，把操作员筛选
   到的分区钉成一份不可变清单；每个分区记录当时钉住的数据版本、行数与校验值，
   同时冻结“应到”行主键集合。清单生成后不再改写。
2. 清单驱动：``execute_reconciliation`` 只读清单指向的分区版本，绝不按批次号
   glob 当前产物文件，因此同一批次的多个分区不会跨版本合读。若清单钉住的是
   未确认快照、执行时该分区已产生确认版本，则整体改用那一份确认版本，并在
   结果里记录 ``used_alternate_version`` 以及钉住版本 -> 实际版本的对应关系。
3. 逐行复算：实到/缺失按记录主键逐行比对，结果落盘为对账快照，历史列表可
   按快照留存的分区与数据版本原样复算，不会随着后续补录而漂移。
"""
from __future__ import annotations

import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class ArrivalReconcileService(BaseModuleService):
    MODULE = "arrival_reconcile"
    ENTITY = "对账单"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["对账编号", "批次编号", "应到条数", "实到条数", "迟到条数", "缺失条数", "对账窗口", "对账结论"]
    REQUIRED_FIELDS = ["对账编号", "批次编号", "应到条数"]
    STATUS_ORDER = ["草稿", "待对账", "对账中", "已平账", "存在差异"]
    ACTION_RULES = {"启动对账": "对账中", "确认平账": "已平账", "标记差异": "存在差异"}
    NEGATIVE_ACTIONS = ["标记差异"]

    # -- 快照目录 --

    @staticmethod
    def _as_int(value: Any, default: int = 0) -> int:
        """安全转整数；示例数据里的占位文本按默认值处理。"""
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _manifest_dir(self):
        return self._data_dir("reconcile_manifests")

    def _manifest_path(self, manifest_code: str):
        return self._manifest_dir() / f"{manifest_code}.json"

    def _snapshot_path(self, recon_code: str):
        return self._data_dir("reconcile_snapshots") / f"{recon_code}.json"

    # -- 可复核清单 --

    def build_review_manifest(
        self,
        batch_code: str,
        *,
        staging_codes: list[str] | None = None,
        include_unlanded: bool = False,
        operator: str = "数据工程组",
        note: str = "",
    ) -> dict[str, Any]:
        """查询批次对应的暂存分区明细，生成可复核对账对象清单。

        每个分区在清单里钉住一个数据版本：
        - 有确认版本时钉确认版本（保留该分区自己的确认结果）；
        - 还在补录、尚未确认时钉当前最新快照，并标记 ``confirmed: false``，
          执行对账时若已产生确认版本，会整体改用确认版本并留下记录。

        清单同时冻结“应到”行主键集合，后续执行/复算都只认这份集合。
        """
        staging_svc = self._safe_import(
            "app.services.staging_area", "StagingAreaService")()
        partitions = staging_svc.list_batch_partitions(batch_code)
        if staging_codes:
            wanted = set(staging_codes)
            partitions = [p for p in partitions if p["staging_code"] in wanted]
        if not include_unlanded:
            partitions = [
                p for p in partitions
                if p.get("status") == "已落地" or p.get("latest_confirmed_version")
            ]
        if not partitions:
            return {"error": f"批次 {batch_code} 下没有可复核的暂存分区（需已落地或已确认）"}

        entries: list[dict[str, Any]] = []
        for part in partitions:
            pinned = staging_svc.pin_for_manifest(part["staging_code"])
            if pinned is None:
                return {"error": f"暂存分区 {part['staging_code']} 没有任何可读取的数据版本"}
            entries.append({
                "staging_code": part["staging_code"],
                "staging_id": part["id"],
                "partition_no": part.get("partition_no", ""),
                "status": part.get("status", ""),
                "data_version": pinned.get("data_version") or f"v{pinned['version']}",
                "version": int(pinned["version"]),
                "record_count": int(pinned.get("record_count", 0)),
                "checksum": pinned.get("checksum", ""),
                "confirmed": bool(pinned.get("confirmed")),
                "confirmed_count": self._as_int(part.get("confirmed_count")),
                "pending_append": bool(part.get("pending_append")),
            })

        # 应到基线优先取各分区登记的应到主键（能识别“应到未到”的缺失行）；
        # 没有登记时退回批次产物文件；再没有则退回清单钉住版本的行集合
        expected_keys = self._declared_expected_keys(batch_code, entries)
        if not expected_keys:
            expected_keys = self._batch_record_keys(batch_code)
        if not expected_keys:
            for entry in entries:
                loaded = staging_svc.read_version(entry["staging_code"], entry["version"])
                if loaded is not None:
                    expected_keys.extend(
                        str(r.get("record_key") or self._record_key(r))
                        for r in loaded["rows"])
        expected_keys = list(dict.fromkeys(expected_keys))

        manifest_code = f"MANI-{batch_code}-{int(time.time() * 1000) % 100000000}"
        existing = {m.get("manifest_code") for m in self.list_manifests(batch_code)}
        if manifest_code in existing:
            manifest_code = f"{manifest_code}-{len(existing) + 1}"
        manifest = {
            "manifest_code": manifest_code,
            "batch_code": batch_code,
            "operator": operator,
            "note": note,
            "created_at": time.time(),
            "created_display": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            "partition_count": len(entries),
            "expected_total": len(expected_keys),
            "expected_row_keys": expected_keys,
            "partitions": entries,
            # 清单自身指纹：任何一个分区的版本号/行数/校验值变化都会改变它
            "manifest_checksum": self._stable_checksum(
                {"partitions": entries, "expected_row_keys": expected_keys}),
        }
        self._write_json_file(self._manifest_path(manifest_code), manifest)
        self._write_jsonl(
            self._data_dir("events") / "reconcile_manifest_log.jsonl",
            [{"action": "build_manifest", "manifest_code": manifest_code,
              "batch_code": batch_code, "partition_count": len(entries),
              "expected_total": len(expected_keys), "at": manifest["created_at"]}]
        )
        return manifest

    def _batch_record_keys(self, batch_code: str) -> list[str]:
        """批次源端产物的记录主键，作为“应到”基线。"""
        batch_file = self._data_dir("batches") / f"{batch_code}.jsonl"
        records = self._read_jsonl(batch_file)
        return [str(r.get("record_key") or self._record_key(r)) for r in records]

    def _declared_expected_keys(
        self, batch_code: str, manifest_partitions: list[dict[str, Any]]
    ) -> list[str]:
        """分区登记时声明的应到主键，用于无上游批次产物时逐行识别缺失。

        只统计清单里出现的分区，且同一批次的多分区按主键去重，
        不会把清单外分区的应到行混进来。
        """
        staging_codes = {p["staging_code"] for p in manifest_partitions}
        keys: list[str] = []
        for row in store.rows("staging_area"):
            if row.get("批次编号") != batch_code:
                continue
            if row.get("暂存编号") not in staging_codes:
                continue
            declared = row.get("应到主键")
            if isinstance(declared, list):
                keys.extend(str(k) for k in declared)
        return list(dict.fromkeys(keys))

    def list_manifests(self, batch_code: str | None = None) -> list[dict[str, Any]]:
        """列出已生成的可复核清单，可按批次过滤，最新在前。"""
        manifests = []
        for path in sorted(self._manifest_dir().glob("MANI-*.json")):
            payload = self._read_json_file(path)
            if not isinstance(payload, dict):
                continue
            if batch_code and payload.get("batch_code") != batch_code:
                continue
            manifests.append(payload)
        manifests.sort(key=lambda m: float(m.get("created_at", 0)), reverse=True)
        return manifests

    def get_manifest(self, manifest_code: str) -> dict[str, Any] | None:
        payload = self._read_json_file(self._manifest_path(manifest_code))
        return payload if isinstance(payload, dict) else None

    # -- 对账单 --

    def create_reconciliation(
        self,
        batch_code: str,
        expected_count: int,
        *,
        manifest_code: str | None = None,
    ) -> dict[str, Any]:
        """为传输批次创建到达对账单，登记应到条数，为后续到账核对准备基线。

        传入 manifest_code 时，应到条数直接采用清单冻结的 expected_total，
        对账单从此绑定该清单，不再受分区后续补录影响。
        """
        manifest = None
        if manifest_code:
            manifest = self.get_manifest(manifest_code)
            if manifest is None:
                return {"error": f"可复核清单 {manifest_code} 不存在"}
            expected_count = int(manifest["expected_total"])
        recon_code = f"ARRI-{batch_code}-{int(time.time() * 1000) % 100000000}"
        values = {
            "对账编号": recon_code,
            "批次编号": batch_code,
            "应到条数": str(expected_count),
            "实到条数": "0",
            "迟到条数": "0",
            "缺失条数": "0",
            "对账窗口": manifest_code or "daily",
            "对账结论": "待对账",
        }
        entry, missing = self.create_entry(values)
        if entry is None:
            raise ValueError(f"创建对账单失败: {missing}")
        return {"recon_code": recon_code, "recon_id": entry["id"], "batch_code": batch_code,
                "expected_count": expected_count, "manifest_code": manifest_code}

    def execute_from_manifest(
        self,
        manifest_code: str,
        *,
        operator: str = "数据工程组",
    ) -> dict[str, Any]:
        """按可复核清单建单并执行对账，返回对账单与逐行结果。"""
        manifest = self.get_manifest(manifest_code)
        if manifest is None:
            return {"error": f"可复核清单 {manifest_code} 不存在"}
        created = self.create_reconciliation(
            manifest["batch_code"], int(manifest["expected_total"]),
            manifest_code=manifest_code)
        if "error" in created:
            return created
        result = self.execute_reconciliation(created["recon_id"])
        if "error" not in result:
            result["manifest_code"] = manifest_code
            result["recon_id"] = created["recon_id"]
        return result

    def _resolve_partition_version(
        self,
        staging_svc: Any,
        pinned: dict[str, Any],
        allow_alternate: bool,
    ) -> tuple[dict[str, Any] | None, int, bool, str]:
        """把清单钉住的分区解析成唯一一个可读的数据版本。

        返回 (版本快照, 实际使用版本号, 是否改用其他确认版本, 说明)。

        策略：
        - 分区已有确认版本、且与清单钉住的版本不同：使用该分区的实际确认版本，
          记录“改用另一个确认版本”。这是筛选提交后分区又确认了新版本的情形。
        - 清单钉住的版本就是最新确认版本：原样使用，保证复核可重现。
        - 分区还没有任何确认版本：使用清单冻结的筛选快照（可能未确认）。

        任何情况下每个分区只返回一个版本的数据行，绝不把两个版本拼在一起。
        """
        staging_code = pinned["staging_code"]
        version = int(pinned["version"])
        pinned_snapshot = staging_svc.read_version(staging_code, version)
        if not allow_alternate:
            return pinned_snapshot, version, False, ""

        index = staging_svc.version_index(staging_code)
        latest_confirmed = index.get("latest_confirmed")
        if latest_confirmed is None:
            # 没有确认版本可用，只能使用清单冻结的筛选快照
            return pinned_snapshot, version, False, ""

        latest_confirmed = int(latest_confirmed)
        latest_snapshot = staging_svc.read_version(staging_code, latest_confirmed)
        if latest_snapshot is None:
            return pinned_snapshot, version, False, ""
        if latest_confirmed == version:
            return pinned_snapshot, version, False, ""

        # 分区在筛选后确认了新版本：改用该分区实际的确认版本，只读这一份
        if pinned_snapshot is None:
            reason = "清单钉住版本不可读，改用该分区最新确认版本"
        elif not pinned_snapshot.get("confirmed"):
            reason = "清单钉住的是未确认筛选快照，改用该分区最新确认版本"
        else:
            reason = "筛选后该分区又确认了新版本，改用最新确认版本"
        return latest_snapshot, latest_confirmed, True, reason

    def execute_reconciliation(self, recon_id: int, *, allow_fallback: bool = True) -> dict[str, Any]:
        """执行到达对账：按清单读取分区版本，根据实际记录生成实到与缺失数据。

        读取顺序固定为：对账单 -> 可复核清单 -> 清单钉住的分区版本。
        清单里没有的分区一律不读；每个分区只解析出一个数据版本，因此同一批次
        的两个分区不会跨版本合读。若本次改用了另一个确认版本，会写入
        ``used_alternate_version`` 与逐分区的版本对应关系。
        """
        recon_entry = self.get_entry(recon_id)
        if recon_entry is None:
            return {"error": f"对账单 {recon_id} 不存在"}

        entry, msg = self.run_action(recon_id, "启动对账")
        if entry is None:
            return {"error": msg, "recon_id": recon_id}

        batch_code = recon_entry["批次编号"]
        manifest_code = recon_entry.get("对账窗口", "")
        manifest = self.get_manifest(manifest_code) if manifest_code.startswith("MANI-") else None
        if manifest is None:
            # 未绑定清单的历史对账单：沿用按批次产物计数的旧口径，明确标记为 legacy
            return self._execute_legacy(recon_id, recon_entry)

        staging_svc = self._safe_import(
            "app.services.staging_area", "StagingAreaService")()

        expected_keys = list(manifest.get("expected_row_keys", []))
        partition_results: list[dict[str, Any]] = []
        actual_keys: list[str] = []
        alternate_used = False
        alternate_details: list[dict[str, Any]] = []

        for pinned in manifest["partitions"]:
            staging_code = pinned["staging_code"]
            snapshot, resolved_version, used_alternate, reason = self._resolve_partition_version(
                staging_svc, pinned, allow_fallback)
            if snapshot is None:
                return self._abort(
                    recon_id,
                    f"清单 {manifest_code} 指向的分区 {staging_code} "
                    f"v{pinned['version']} 不可读，为免合读其他版本，本次对账中止")

            rows = snapshot["rows"]
            keys = [str(r.get("record_key") or self._record_key(r)) for r in rows]
            actual_keys.extend(keys)
            pinned_snapshot = staging_svc.read_version(staging_code, int(pinned["version"]))
            confirmation = self._partition_confirmation(staging_svc, staging_code)
            available_versions = self._partition_versions(staging_svc, staging_code)
            partition_results.append({
                "staging_code": staging_code,
                "staging_id": pinned.get("staging_id"),
                "partition_no": pinned.get("partition_no", ""),
                "pinned_version": int(pinned["version"]),
                "pinned_data_version": pinned.get("data_version", ""),
                "pinned_record_count": self._as_int(pinned.get("record_count")),
                "pinned_checksum": (pinned_snapshot or {}).get("checksum", pinned.get("checksum", "")),
                "pinned_confirmed": (pinned_snapshot or {}).get("confirmed", None),
                "data_version": f"v{resolved_version}",
                "resolved_version": resolved_version,
                "actual_count": len(rows),
                "used_alternate_version": used_alternate,
                "alternate_reason": reason,
                "checksum": snapshot.get("checksum", ""),
                "confirmed": bool(snapshot.get("confirmed")),
                "record_keys": keys,
                # 两份确认结果分别与实际分区关联，历史列表可逐个打开
                "confirmation": confirmation,
                "used_confirmation": {
                    "staging_code": staging_code,
                    "confirmed_version": resolved_version,
                    "record_count": len(rows),
                    "checksum": snapshot.get("checksum", ""),
                    "confirmed": bool(snapshot.get("confirmed")),
                },
                # 对账时刻该分区全部可打开的版本（含 4 行版与 5 行版），供历史列表逐个打开
                "available_versions": available_versions,
            })
            if used_alternate:
                alternate_used = True
                alternate_details.append({
                    "staging_code": staging_code,
                    "partition_no": pinned.get("partition_no", ""),
                    "pinned_version": int(pinned["version"]),
                    "used_version": resolved_version,
                    "reason": reason,
                })

        actual_set = list(dict.fromkeys(actual_keys))
        expected_set = list(dict.fromkeys(expected_keys))
        if not expected_set:
            expected_set = actual_set
        expected_lookup = set(expected_set)
        actual_lookup = set(actual_set)
        arrived = [k for k in expected_set if k in actual_lookup]
        missing = [k for k in expected_set if k not in actual_lookup]
        extra = [k for k in actual_set if k not in expected_lookup]

        expected_total = len(expected_set)
        actual_total = len(actual_set)
        missing_count = len(missing)
        late_count = missing_count
        is_balanced = expected_total == actual_total and not missing and not extra

        entry["应到条数"] = str(expected_total)
        entry["实到条数"] = str(actual_total)
        entry["迟到条数"] = str(late_count)
        entry["缺失条数"] = str(missing_count)

        result_action = "确认平账" if is_balanced else "标记差异"
        self.run_action(recon_id, result_action)

        recon_code = recon_entry["对账编号"]
        snapshot_payload = {
            "recon_code": recon_code,
            "batch_code": batch_code,
            "manifest_code": manifest_code,
            "manifest_checksum": manifest.get("manifest_checksum", ""),
            "manifest_created_at": manifest.get("created_at"),
            "expected_total": expected_total,
            "actual_total": actual_total,
            "late": late_count,
            "missing": missing_count,
            "extra": len(extra),
            "balanced": is_balanced,
            "used_alternate_version": alternate_used,
            "alternate_details": alternate_details,
            "partitions": partition_results,
            "expected_row_keys": expected_set,
            "actual_row_keys": actual_set,
            "arrived_row_keys": arrived,
            "missing_row_keys": missing,
            "extra_row_keys": extra,
            "reconciled_at": time.time(),
            "reconciled_display": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        }
        self._write_json_file(self._snapshot_path(recon_code), snapshot_payload)
        self._write_jsonl(
            self._data_dir("events") / "reconcile_log.jsonl",
            [{"action": "reconcile", "batch_code": batch_code, "manifest_code": manifest_code,
              "expected": expected_total, "actual": actual_total, "late": late_count,
              "missing": missing_count, "used_alternate_version": alternate_used,
              "balanced": is_balanced, "reconciled_at": time.time()}]
        )

        result = {
            "recon_code": recon_code,
            "recon_id": recon_id,
            "batch_code": batch_code,
            "manifest_code": manifest_code,
            "expected": expected_total,
            "actual": actual_total,
            "late": late_count,
            "missing": missing_count,
            "balanced": is_balanced,
            "used_alternate_version": alternate_used,
            "alternate_details": alternate_details,
            "partitions": partition_results,
            "missing_row_keys": missing,
            "extra_row_keys": extra,
        }

        # 对账结果驱动时效监控指标更新
        if late_count > 0 or not is_balanced:
            try:
                sm_svc = self._safe_import("app.services.sla_monitor", "SlaMonitorService")()
                sm_result = sm_svc.update_arrival_stats(batch_code, late_count, missing_count)
                result["auto_sla_update"] = sm_result
            except Exception as e:
                result["auto_sla_update"] = {"error": str(e)}

        # 对账完成后记录传输血缘
        try:
            tl_svc = self._safe_import("app.services.transfer_lineage", "TransferLineageService")()
            lineage_result = tl_svc.record_lineage(batch_code, is_balanced)
            result["auto_lineage"] = lineage_result
        except Exception as e:
            result["auto_lineage"] = {"error": str(e)}

        return result

    def _partition_versions(self, staging_svc: Any, staging_code: str) -> list[dict[str, Any]]:
        """对账时刻该分区全部可打开的版本摘要，历史列表据此分别打开两份确认结果。"""
        versions = []
        for version in staging_svc.list_versions(staging_code):
            versions.append({
                "staging_code": staging_code,
                "version": int(version["version"]),
                "data_version": version.get("data_version") or f"v{version['version']}",
                "record_count": self._as_int(version.get("record_count")),
                "checksum": version.get("checksum", ""),
                "confirmed": bool(version.get("confirmed")),
                "kind": version.get("kind", ""),
                "created_at": version.get("created_at"),
            })
        return versions

    def _partition_confirmation(self, staging_svc: Any, staging_code: str) -> dict[str, Any] | None:
        """取分区最新一份确认结果及全部确认版本，关联“确认结果 <-> 实际分区”。

        同一分区可能存在多份确认结果（4 行一版、补到 5 行后再确认一版）；
        这里全部保留，历史列表可以分别打开对比，而不是只留最后一版。
        """
        index = staging_svc.version_index(staging_code)
        confirmed = [v for v in index.get("versions", []) if v.get("confirmed")]
        if not confirmed:
            return None
        confirmed.sort(key=lambda v: int(v["version"]))
        latest = confirmed[-1]
        return {
            "staging_code": staging_code,
            "confirmed_version": int(latest["version"]),
            "record_count": self._as_int(latest.get("record_count")),
            "checksum": latest.get("checksum", ""),
            "confirmed_at": latest.get("created_at"),
            "confirmations": [
                {
                    "staging_code": staging_code,
                    "confirmed_version": int(v["version"]),
                    "data_version": v.get("data_version") or f"v{v['version']}",
                    "record_count": self._as_int(v.get("record_count")),
                    "checksum": v.get("checksum", ""),
                    "confirmed_at": v.get("created_at"),
                }
                for v in confirmed
            ],
        }

    def _abort(self, recon_id: int, reason: str) -> dict[str, Any]:
        """对账中止：不产出实到/缺失数据，明确写入失败原因。"""
        entry, _ = super().run_action(recon_id, "标记差异")
        if entry is not None:
            entry["缺失条数"] = "0"
        return {"error": reason, "recon_id": recon_id}

    def _execute_legacy(
        self, recon_id: int, recon_entry: dict[str, Any]
    ) -> dict[str, Any]:
        """兼容未绑定可复核清单的历史对账单：按批次产物计数，不做逐行版本钉住。

        新链路一律走清单；这个分支只为已建但未绑定清单的旧单保留可执行性，
        结果里用 ``manifest_code: ""`` 与 ``legacy: true`` 明确区分。
        """
        batch_code = recon_entry["批次编号"]
        expected = self._as_int(recon_entry.get("应到条数"))
        batch_records = self._read_jsonl(self._data_dir("batches") / f"{batch_code}.jsonl")
        expected = expected or len(batch_records)
        staging_svc = self._safe_import(
            "app.services.staging_area", "StagingAreaService")()
        partitions = staging_svc.list_batch_partitions(batch_code)
        actual_count = sum(self._as_int(p.get("record_count")) for p in partitions)
        missing = max(0, expected - actual_count)
        is_balanced = expected == actual_count and bool(partitions)

        entry = self.get_entry(recon_id)
        if entry is not None:
            entry["实到条数"] = str(actual_count)
            entry["迟到条数"] = str(missing)
            entry["缺失条数"] = str(missing)
        self.run_action(recon_id, "确认平账" if is_balanced else "标记差异")

        result: dict[str, Any] = {
            "recon_code": recon_entry["对账编号"],
            "recon_id": recon_id,
            "batch_code": batch_code,
            "manifest_code": "",
            "legacy": True,
            "expected": expected,
            "actual": actual_count,
            "late": missing,
            "missing": missing,
            "balanced": is_balanced,
            "used_alternate_version": False,
            "partitions": [
                {"staging_code": p["staging_code"], "partition_no": p.get("partition_no"),
                 "data_version": p.get("data_version", "")}
                for p in partitions
            ],
        }
        if missing > 0 or not is_balanced:
            try:
                sm_svc = self._safe_import("app.services.sla_monitor", "SlaMonitorService")()
                result["auto_sla_update"] = sm_svc.update_arrival_stats(
                    batch_code, missing, missing)
            except Exception as e:
                result["auto_sla_update"] = {"error": str(e)}
        try:
            tl_svc = self._safe_import("app.services.transfer_lineage", "TransferLineageService")()
            result["auto_lineage"] = tl_svc.record_lineage(batch_code, is_balanced)
        except Exception as e:
            result["auto_lineage"] = {"error": str(e)}
        return result

    # -- 历史复算 --

    def get_reconciliation_snapshot(self, recon_id: int) -> dict[str, Any] | None:
        """按对账单读取留存的分区与逐行结果快照。"""
        entry = self.get_entry(recon_id)
        if entry is None:
            return None
        payload = self._read_json_file(self._snapshot_path(entry["对账编号"]))
        return payload if isinstance(payload, dict) else None

    def recompute_reconciliation(self, recon_id: int) -> dict[str, Any]:
        """按留存的分区版本逐行复算实到和缺失，验证与历史结论一致。

        复算只读快照里记录的每个分区的 data_version，不读当前产物文件；
        这样即使分区后来又补录到第 5 行，第 4 行那一版的对账仍能原样重现。
        """
        entry = self.get_entry(recon_id)
        if entry is None:
            return {"error": f"对账单 {recon_id} 不存在"}
        snapshot = self.get_reconciliation_snapshot(recon_id)
        if snapshot is None:
            return {"error": f"对账单 {entry['对账编号']} 没有留存快照，无法复算"}
        staging_svc = self._safe_import(
            "app.services.staging_area", "StagingAreaService")()

        actual_keys: list[str] = []
        partition_results = []
        for part in snapshot.get("partitions", []):
            version = int(part.get("resolved_version") or str(part["data_version"]).lstrip("v"))
            loaded = staging_svc.read_version(part["staging_code"], version)
            if loaded is None:
                return {"error": f"留存分区 {part['staging_code']} v{version} 不可读，无法复算"}
            keys = [str(r.get("record_key") or self._record_key(r)) for r in loaded["rows"]]
            actual_keys.extend(keys)
            partition_results.append({
                "staging_code": part["staging_code"],
                "partition_no": part.get("partition_no", ""),
                "data_version": f"v{version}",
                "record_count": len(keys),
                "checksum": loaded.get("checksum", ""),
                "matches_pinned_checksum": loaded.get("checksum", "") == part.get("checksum", ""),
            })
        actual_set = list(dict.fromkeys(actual_keys))
        expected_keys = list(snapshot.get("expected_row_keys", []))
        arrived = [k for k in expected_keys if k in set(actual_set)]
        missing = [k for k in expected_keys if k not in set(actual_set)]
        recomputed = {
            "recon_code": entry["对账编号"],
            "batch_code": entry["批次编号"],
            "manifest_code": snapshot.get("manifest_code", ""),
            "expected": len(expected_keys),
            "actual": len(actual_set),
            "missing": len(missing),
            "arrived": len(arrived),
            "used_alternate_version": bool(snapshot.get("used_alternate_version")),
            "partitions": partition_results,
            "matches_snapshot": (
                len(missing) == int(snapshot.get("missing", -1))
                and len(actual_set) == int(snapshot.get("actual_total", -1))
                and all(p["matches_pinned_checksum"] for p in partition_results)
            ),
            "recomputed_at": time.time(),
        }
        self._write_jsonl(
            self._data_dir("events") / "reconcile_recompute_log.jsonl",
            [{"action": "recompute", "recon_code": entry["对账编号"],
              "matches_snapshot": recomputed["matches_snapshot"], "at": time.time()}]
        )
        return recomputed
