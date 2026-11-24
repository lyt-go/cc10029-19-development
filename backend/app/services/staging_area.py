"""暂存落地领域服务：维护传输数据的暂存分区落地路径、文件组织与驻留时限控制。

本模块从传输批次产物文件读取数据，执行分区落地写入暂存产物文件，
落地完成后记录落地水位并支持超时清空。

同一批次可以落成多个暂存分区。每次“确认落地”都会把当次分区数据固化为
一个不可变版本文件；后续补录只追加新的数据版本，旧版本文件永不覆盖，
因此到达对账可以按清单钉住的分区版本逐行复现，不会把两个版本合读。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class StagingAreaService(BaseModuleService):
    MODULE = "staging_area"
    ENTITY = "暂存分区"
    OPERATION_TERM = "水位"
    LIST_FIELDS = ["暂存编号", "批次编号", "落地路径", "文件数量", "记录条数", "占用字节", "驻留时限", "落地水位"]
    REQUIRED_FIELDS = ["暂存编号", "批次编号", "落地路径"]
    STATUS_ORDER = ["草稿", "落地中", "已落地", "已清空"]
    ACTION_RULES = {"启动落地": "落地中", "确认落地": "已落地", "执行清空": "已清空"}
    NEGATIVE_ACTIONS = ["执行清空"]

    # -- 路径与版本索引 --

    @staticmethod
    def _as_int(value: Any, default: int = 0) -> int:
        """把登记字段安全转成整数；示例数据里的占位文本按默认值处理。"""
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _artifact_path(self, staging_code: str) -> Path:
        """分区当前落地产物（可变，补录会追加到这里）。"""
        return self._data_dir("staging") / f"{staging_code}.jsonl"

    def _version_dir(self, staging_code: str) -> Path:
        """分区版本快照目录，每个版本一个不可变 jsonl + meta。"""
        return self._data_dir("staging_versions") / staging_code

    def _version_path(self, staging_code: str, version: int) -> Path:
        return self._version_dir(staging_code) / f"v{int(version)}.jsonl"

    def _version_meta_path(self, staging_code: str, version: int) -> Path:
        return self._version_dir(staging_code) / f"v{int(version)}.meta.json"

    def _version_index_path(self, staging_code: str) -> Path:
        return self._version_dir(staging_code) / "index.json"

    def version_index(self, staging_code: str) -> dict[str, Any]:
        """读取分区版本索引，包含全部版本元数据与最新确认版本号。"""
        payload = self._read_json_file(self._version_index_path(staging_code))
        if not isinstance(payload, dict):
            payload = {"staging_code": staging_code, "versions": [], "latest_confirmed": None}
        payload.setdefault("versions", [])
        payload.setdefault("latest_confirmed", None)
        return payload

    def list_versions(self, staging_code: str) -> list[dict[str, Any]]:
        """按版本号列出分区快照版本，供历史列表逐个打开。"""
        return sorted(self.version_index(staging_code)["versions"], key=lambda v: int(v["version"]))

    def read_version(self, staging_code: str, version: int) -> dict[str, Any] | None:
        """读取某个分区版本的完整数据行与元数据；版本文件不可变，读到的就是冻结内容。"""
        meta = self._read_json_file(self._version_meta_path(staging_code, version))
        path = self._version_path(staging_code, version)
        if not isinstance(meta, dict) or not path.exists():
            return None
        rows = self._read_jsonl(path)
        return {**meta, "rows": rows, "artifact_path": str(path)}

    def _next_version_no(self, staging_code: str) -> int:
        index = self.version_index(staging_code)
        return max((int(v.get("version", 0)) for v in index["versions"]), default=0) + 1

    def snapshot_version(
        self,
        staging_code: str,
        rows: list[dict[str, Any]],
        *,
        kind: str,
        confirmed: bool,
        source_version: int | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        """把一批分区数据冻结成新的不可变版本。

        kind 为“确认落地”或“筛选快照”：前者代表操作员确认过的行；后者代表
        提交筛选清单那一刻的数据。版本文件写一次就不再改动，补录只会产生下一个版本。
        """
        version = self._next_version_no(staging_code)
        normalized = []
        for row in rows:
            record = dict(row)
            record.setdefault("record_key", self._record_key(record))
            normalized.append(record)
        version_path = self._version_path(staging_code, version)
        self._write_jsonl_replace(version_path, normalized)
        checksum = self._stable_checksum(normalized)
        staging_row = next(
            (r for r in store.rows(self.MODULE) if r.get("暂存编号") == staging_code), {})
        meta = {
            "version": version,
            "staging_code": staging_code,
            "batch_code": staging_row.get("批次编号", ""),
            "partition_no": staging_row.get("分区序号", ""),
            "kind": kind,
            "confirmed": bool(confirmed),
            "record_count": len(normalized),
            "checksum": checksum,
            "data_version": f"v{version}",
            "source_version": source_version,
            "note": note,
            "created_at": time.time(),
            "artifact_path": str(version_path),
        }
        self._write_json_file(self._version_meta_path(staging_code, version), meta)
        index = self.version_index(staging_code)
        index["versions"].append(meta)
        if confirmed:
            index["latest_confirmed"] = version
            staging_row["确认版本"] = f"v{version}"
            staging_row["确认记录数"] = str(len(normalized))
            staging_row["确认时间"] = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(meta["created_at"]))
        staging_row["数据版本"] = f"v{version}"
        self._write_json_file(self._version_index_path(staging_code), index)
        return meta

    # -- 落地与补录 --

    def _next_partition_no(self, batch_code: str) -> int:
        existing = [
            self._as_int(row.get("分区序号"))
            for row in store.rows(self.MODULE)
            if row.get("批次编号") == batch_code
        ]
        return max(existing, default=0) + 1

    def land_batch(
        self,
        batch_code: str,
        record_count: int,
        partition_no: int | None = None,
        *,
        start_index: int | None = None,
        rows: list[dict[str, Any]] | None = None,
        expected_count: int | None = None,
    ) -> dict[str, Any]:
        """将传输批次数据落地到暂存分区，读取传输产物文件并写入暂存产物文件。

        暂存分区是传输链路中数据暂存的关键环节：它从传输产物文件读取数据，
        计算字节量，写入落地路径下的暂存产物文件，并推进分区水位。
        同一批次多次调用会产生 P1、P2... 多个分区，各自持有独立产物和版本链；
        用 ``start_index`` 指定本分区从批次产物的第几行开始，不传则从首行开始。
        多分区落地时调用方应显式传 ``start_index`` 让分区不重叠。
        ``expected_count`` 登记该分区应到行数（可与实际落地行数不同，用于识别缺失）。
        """
        part_no = partition_no or self._next_partition_no(batch_code)
        if start_index is None:
            start_index = 0
        staging_code = f"STAG-{batch_code}-P{part_no}"
        if any(r.get("暂存编号") == staging_code for r in store.rows(self.MODULE)):
            staging_code = f"{staging_code}-{int(time.time())}"
        landing_path = f"/staging/{batch_code}/P{part_no}/"
        values = {
            "暂存编号": staging_code,
            "批次编号": batch_code,
            "落地路径": landing_path,
            "文件数量": "1",
            "记录条数": str(record_count),
            "占用字节": "0",
            "驻留时限": "86400",
            "落地水位": "0",
        }
        entry, missing = self.create_entry(values)
        if entry is None:
            raise ValueError(f"创建暂存分区失败: {missing}")
        entry["分区序号"] = str(part_no)
        entry["数据版本"] = "未快照"
        entry["确认版本"] = ""
        entry["确认记录数"] = "0"
        entry["确认时间"] = ""
        entry["待确认补录"] = "否"
        if expected_count is not None:
            entry["应到条数"] = str(expected_count)
            entry["分区起点"] = str(start_index)
            entry["应到主键"] = [
                f"{batch_code}:{start_index + i + 1}" for i in range(max(0, expected_count))
            ]
        else:
            # 未显式登记应到行数时，以实际落地的行为基线，缺失数按清单内实际行计算
            entry["应到条数"] = str(record_count)
            entry["分区起点"] = str(start_index)
            entry["应到主键"] = [
                f"{batch_code}:{start_index + i + 1}" for i in range(max(0, record_count))
            ]

        # 读取上游传输批次产物文件
        source_file = self._data_dir("batches") / f"{batch_code}.jsonl"
        total_bytes = 0
        if rows is not None:
            records = [dict(row) for row in rows]
        else:
            source_records = self._read_jsonl(source_file)
            if source_records:
                end = start_index + record_count if record_count else None
                records = source_records[start_index:end]
            else:
                records = self._simulate_records(batch_code, count=record_count, field_prefix="staging")
                # 没有批次产物时按分区起点续排行号，保证不同分区的主键不重叠
                for i, row in enumerate(records):
                    row["line_no"] = start_index + i + 1
                    row["record_key"] = f"{batch_code}:{start_index + i + 1}"
        for row in records:
            row.setdefault("record_key", self._record_key(row))
        total_bytes = sum(len(str(r).encode("utf-8")) for r in records)

        # 写入暂存产物文件（分区独立文件，不与其他分区混写）
        output_file = self._artifact_path(staging_code)
        written = self._write_jsonl_replace(output_file, records)

        # 更新暂存分区水位
        entry["占用字节"] = str(total_bytes)
        entry["文件数量"] = "1"
        entry["记录条数"] = str(written)
        entry["落地水位"] = str(written)
        entry["数据版本"] = f"v{self._next_version_no(staging_code)}"
        # 写入落地水位断点
        self._write_checkpoint(f"staging_{staging_code}", {
            "staging_code": staging_code,
            "batch_code": batch_code,
            "partition_no": part_no,
            "records_landed": written,
            "total_bytes": total_bytes,
            "landed_at": time.time(),
            "retention_seconds": 86400,
        })

        return {
            "staging_code": staging_code,
            "staging_id": entry["id"],
            "batch_code": batch_code,
            "partition_no": part_no,
            "records_landed": written,
            "total_bytes": total_bytes,
            "artifact_path": str(output_file),
        }

    def append_rows(
        self,
        staging_id: int,
        count: int = 1,
        rows: list[dict[str, Any]] | None = None,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """向已落地分区补录迟到数据（例如第 5 行），并登记新的数据版本。

        补录只改当前产物文件，不会改写任何历史确认版本；如需让补录进入
        版本链，必须重新“确认落地”生成下一个确认版本。
        """
        entry = self.get_entry(staging_id)
        if entry is None:
            return {"error": f"暂存分区 {staging_id} 不存在"}
        staging_code = entry["暂存编号"]
        artifact = self._artifact_path(staging_code)
        existing = self._read_jsonl(artifact)
        if rows is None:
            new_rows = self._simulate_records(
                entry["批次编号"], count=max(1, count), field_prefix="staging")
            # 行号必须接着分区已有的全局行号续排，不能按当前行数重新从 1 数，
            # 否则补录行会与已确认版本的行主键重复，逐行复算会少算一行。
            next_line = max(
                (self._as_int(existing_row.get("line_no")) for existing_row in existing),
                default=0,
            ) + 1
            for i, row in enumerate(new_rows):
                row["line_no"] = next_line + i
                row["record_key"] = f"{entry['批次编号']}:{next_line + i}"
        else:
            new_rows = [dict(row) for row in rows]
        # 补录行必须带记录主键，否则无法与已确认版本逐行比对
        for row in new_rows:
            row.setdefault("record_key", self._record_key(row))
        combined = existing + new_rows
        written = self._write_jsonl_replace(artifact, combined)
        entry["记录条数"] = str(written)
        entry["落地水位"] = str(written)
        entry["待确认补录"] = "是"
        data_version = f"v{self._next_version_no(staging_code)}"
        entry["数据版本"] = data_version
        meta = None
        if confirm:
            meta = self._confirm_land_version(staging_id)
            entry["数据版本"] = self.get_entry(staging_id).get("数据版本", data_version)
            data_version = entry["数据版本"]
        return {
            "staging_code": staging_code,
            "staging_id": staging_id,
            "appended": len(new_rows),
            "record_count": written,
            "data_version": data_version,
            "confirmed_version": meta,
        }

    # -- 确认落地：生成不可变版本 --

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """执行分区动作；“确认落地”在改状态的同时冻结一个确认版本。"""
        if action == "确认落地":
            entry = self.get_entry(entry_id)
            if entry is None:
                return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
            meta = self._confirm_land_version(entry_id)
            if meta is None:
                return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
            entry = self.get_entry(entry_id)
            return entry, f"{self.ENTITY}已确认落地（版本 v{meta['version']}，{meta['record_count']} 行）"
        return super().run_action(entry_id, action)

    def _confirm_land_version(self, staging_id: int) -> dict[str, Any] | None:
        """执行确认落地：状态推进 + 当前产物冻结为确认版本。"""
        entry, _ = super().run_action(staging_id, "确认落地")
        if entry is None:
            return None
        staging_code = entry["暂存编号"]
        rows = self._read_jsonl(self._artifact_path(staging_code))
        if not rows:
            # 历史样例分区没有产物文件时，以登记条数生成占位行，保证版本可复核
            expected_rows = self._as_int(entry.get("记录条数"))
            rows = self._simulate_records(staging_code, count=expected_rows, field_prefix="staging")
            self._write_jsonl_replace(self._artifact_path(staging_code), rows)
        meta = self.snapshot_version(
            staging_code, rows, kind="确认落地", confirmed=True,
            note="操作员确认落地")
        entry["待确认补录"] = "否"
        self._write_jsonl(
            self._data_dir("events") / "staging_confirm_log.jsonl",
            [{"action": "confirm_land", "staging_code": staging_code,
              "version": meta["version"], "record_count": meta["record_count"],
              "checksum": meta["checksum"], "confirmed_at": time.time()}]
        )
        return meta

    def confirm_land(self, staging_id: int) -> dict[str, Any]:
        """确认暂存分区落地完成，冻结确认版本并写入落地确认日志。"""
        meta = self._confirm_land_version(staging_id)
        if meta is None:
            return {"error": f"暂存分区 {staging_id} 不存在", "staging_id": staging_id}
        return {"staging_code": meta["staging_code"], "status": "已落地",
                "version": meta["version"], "record_count": meta["record_count"],
                "checksum": meta["checksum"]}

    def read_current(self, staging_code: str) -> list[dict[str, Any]]:
        """读取分区当前产物行（可能包含尚未确认的补录）。"""
        return self._read_jsonl(self._artifact_path(staging_code))

    def pin_for_manifest(
        self, staging_code: str, *, note: str = "筛选清单快照"
    ) -> dict[str, Any]:
        """已有确认版本时先钉确认版本，否则才现场冻结筛选快照。

        复核清单优先钉操作员确认过的结果；只有分区还在补录、尚未确认时，
        才在筛选瞬间冻结一份快照，并在清单里标记 ``confirmed: false``。
        """
        index = self.version_index(staging_code)
        if index.get("latest_confirmed"):
            confirmed = [
                v for v in index["versions"]
                if int(v["version"]) == int(index["latest_confirmed"])]
            if confirmed:
                return confirmed[0]
        # 分区尚未确认：复用最新筛选快照，避免每次查询都新增版本
        versions = index.get("versions", [])
        if versions and versions[-1].get("kind") == "筛选快照":
            return versions[-1]
        rows = self.read_current(staging_code)
        for row in rows:
            row.setdefault("record_key", self._record_key(row))
        return self.snapshot_version(
            staging_code, rows, kind="筛选快照", confirmed=False, note=note)

    def partition_detail(self, staging_id: int) -> dict[str, Any] | None:
        """分区明细：当前状态、数据版本、确认版本以及完整版本列表。"""
        entry = self.get_entry(staging_id)
        if entry is None:
            return None
        staging_code = entry["暂存编号"]
        versions = self.list_versions(staging_code)
        latest = versions[-1] if versions else None
        confirmed = [v for v in versions if v.get("confirmed")]
        return {
            "id": entry["id"],
            "staging_code": staging_code,
            "batch_code": entry.get("批次编号", ""),
            "partition_no": entry.get("分区序号", ""),
            "status": entry.get("status", ""),
            "data_version": entry.get("数据版本", ""),
            "confirmed_version": entry.get("确认版本", ""),
            "confirmed_count": self._as_int(entry.get("确认记录数")),
            "record_count": self._as_int(entry.get("记录条数")),
            "pending_append": entry.get("待确认补录") == "是",
            "versions": versions,
            "latest_version": latest,
            "latest_confirmed_version": confirmed[-1] if confirmed else None,
        }

    def list_batch_partitions(self, batch_code: str) -> list[dict[str, Any]]:
        """查询批次对应的全部暂存分区明细；这是复核清单的筛选入口。"""
        partitions = []
        for row in store.rows(self.MODULE):
            if str(row.get("批次编号", "")) != batch_code:
                continue
            detail = self.partition_detail(int(row["id"]))
            if detail is not None:
                partitions.append(detail)
        partitions.sort(key=lambda p: (self._as_int(p.get("partition_no")), int(p["id"])))
        return partitions

    def clear_staging(self, staging_id: int) -> dict[str, Any]:
        """执行暂存分区清空，删除落地产物文件并写入清空日志。

        已确认的不可变版本仍然保留，历史复核不会因为当前分区被清空而失真。
        """
        entry, msg = super().run_action(staging_id, "执行清空")
        if entry is None:
            return {"error": msg, "staging_id": staging_id}
        staging_code = entry["暂存编号"]
        # 标记清空断点
        self._write_checkpoint(f"staging_{staging_code}_cleared", {
            "staging_code": staging_code,
            "cleared_at": time.time(),
            "status": "cleared",
            "retained_versions": len(self.list_versions(staging_code)),
        })
        self._write_jsonl(
            self._data_dir("events") / "staging_clear_log.jsonl",
            [{"action": "clear", "staging_code": staging_code, "cleared_at": time.time()}]
        )
        return {"staging_code": staging_code, "status": "已清空"}