"""POST /api/regions/batch/preview 集成测试，需要可连接的 PostgreSQL（DATABASE_URL）。

运行：
    DATABASE_URL=postgresql://restrict@localhost:5432/restrict \
        python3 tests/test_batch_preview_api.py
脚本会清空 regions/region_meta/passes/commitments/observations/
observation_events/idempotency_keys/region_change_audit 后跑全套用例，
结束时再次清空，避免影响其它测试脚本。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.main import app  # noqa: E402

failures: list[str] = []


def check(label, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}{(' — ' + detail) if detail and not cond else ''}")
    if not cond:
        failures.append(label)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def at(day: int, h: int, m: int = 0, s: int = 0) -> datetime:
    """2030 年 6 月某日的固定 UTC 时刻（全部远在未来，不受墙钟流逝影响）。"""
    return datetime(2030, 6, day, h, m, s, tzinfo=timezone.utc)


SQUARE = [[0, 0], [100, 0], [100, 100], [0, 100]]
BOX2 = [[200, 0], [300, 0], [300, 100], [200, 100]]
# 拟登记区域 D：拦截 y=200 的通行轨迹/承诺行程（现有区域均在 y∈[0,100]）
BOX_D = [[0, 150], [100, 150], [100, 250], [0, 250]]

# 绝对时刻轨迹（2030-06-01，远在未来）
# 高空 y=200：现有区域均不接触；拟登记 D（y∈[150,250]）会拦截
T_HIGH = [
    {"x": -50, "y": 200, "t": "2030-06-01T10:00:00Z"},
    {"x": 950, "y": 200, "t": "2030-06-01T10:10:00Z"},
]
# 直穿 A 区：x=0@15s、x=100@45s → 穿越 [10:00:15, 10:00:45]
T_CROSS = [
    {"x": -50, "y": 50, "t": "2030-06-01T10:00:00Z"},
    {"x": 150, "y": 50, "t": "2030-06-01T10:01:00Z"},
]
# 更高空 y=400：连拟登记 D 也不接触
T_CLEAR = [
    {"x": -50, "y": 400, "t": "2030-06-01T10:00:00Z"},
    {"x": 950, "y": 400, "t": "2030-06-01T10:10:00Z"},
]

db.init_pool()
client = TestClient(app)


def reset_all():
    with db.pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "TRUNCATE passes, commitments, observations, observation_events"
                " RESTART IDENTITY"
            )
            cur.execute("TRUNCATE regions RESTART IDENTITY")
            cur.execute("TRUNCATE region_change_audit")
            cur.execute("TRUNCATE idempotency_keys")
            cur.execute("UPDATE region_meta SET revision = 0 WHERE id = 1")
        conn.commit()


def table_count(table: str) -> int:
    with db.pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {table}")
            return cur.fetchone()[0]


def create_region(name, polygon, vf, vt):
    return client.post("/api/regions", json={
        "name": name, "polygon": polygon,
        "valid_from": iso(vf), "valid_to": iso(vt),
    })


def preview(expected_revision, operations, trajectories):
    return client.post("/api/regions/batch/preview", json={
        "expected_revision": expected_revision,
        "operations": operations,
        "trajectories": trajectories,
    })


reset_all()

# ====================================================================
# 0. 准备两个现有区域：A[0,100]×[0,100]、B[200,300]×[0,100]
# ====================================================================
r = create_region("A区", SQUARE, at(1, 0), at(3, 0))
check("登记 A 区 201", r.status_code == 201, r.text)
A = r.json()["region"]["id"]
r = create_region("B区", BOX2, at(1, 0), at(3, 0))
check("登记 B 区 201", r.status_code == 201, r.text)
B = r.json()["region"]["id"]
check("当前 revision=2", client.get("/api/revision").json()["revision"] == 2)

# ====================================================================
# 1. 基本预演：版本、逐轨迹安全结论与穿越明细（修订操作）
# ====================================================================
r = preview(2, [{"action": "update", "id": A, "name": "A区-改名"}], [T_HIGH, T_CROSS])
check("基本预演 200", r.status_code == 200, r.text)
p = r.json()
check("报告 revision=2 且带基准时刻 computed_at",
      p["revision"] == 2 and bool(p["computed_at"]), str(p))
check("操作回显：update 带 region_id 与修订后 version=2",
      p["operations"] == [{"index": 0, "action": "update", "region_id": A, "version": 2}],
      str(p["operations"]))
t0, t1 = p["trajectories"]
check("轨迹0（高空）安全", t0["index"] == 0 and t0["safe"] is True, str(t0))
check("轨迹1（穿 A）不安全", t1["index"] == 1 and t1["safe"] is False, str(t1))
res_a = next(x for x in t1["results"] if x["region_id"] == A)
check("A 区穿越明细 10:00:15~10:00:45，operation_index=0",
      res_a["operation_index"] == 0
      and res_a["crossings"][0]["entry_time"] == "2030-06-01T10:00:15Z"
      and res_a["crossings"][0]["exit_time"] == "2030-06-01T10:00:45Z", str(res_a))
res_b = next(x for x in t1["results"] if x["region_id"] == B)
check("未涉及区域 B：crossings 为空且 operation_index 为 null",
      res_b["crossings"] == [] and res_b["operation_index"] is None, str(res_b))
check("存在不安全轨迹 → committable=false", p["committable"] is False, str(p))
check("无凭证/承诺/观察时三个影响列表为空",
      p["conflicting_passes"] == [] and p["conflicting_commitments"] == []
      and p["observation_flips"] == [], str(p))

r = preview(2, [{"action": "update", "id": A, "name": "A区-改名"}], [T_HIGH])
check("全部轨迹安全 → committable=true", r.json()["committable"] is True, r.text)

# ====================================================================
# 2. 拟登记区域以操作下标标识，不分配正式编号
# ====================================================================
ops = [
    {"action": "create", "name": "D区", "polygon": BOX_D,
     "valid_from": iso(at(1, 0)), "valid_to": iso(at(2, 0))},
    {"action": "create", "name": "E区",
     "polygon": [[400, 150], [500, 150], [500, 250], [400, 250]],
     "valid_from": iso(at(1, 0)), "valid_to": iso(at(2, 0))},
]
r = preview(2, ops, [T_HIGH])
p = r.json()
check("两个 create 的预演 200", r.status_code == 200, r.text)
check("create 操作回显 region_id=null、version=1",
      p["operations"] == [
          {"index": 0, "action": "create", "region_id": None, "version": 1},
          {"index": 1, "action": "create", "region_id": None, "version": 1},
      ], str(p["operations"]))
t0 = p["trajectories"][0]
check("高空轨迹被拟登记 D/E 拦截 → unsafe", t0["safe"] is False, str(t0))
# T_HIGH：(-50,200)→(950,200) 600s；x=0@30s、x=100@90s；x=400@270s、x=500@330s
res_d = next(x for x in t0["results"] if x["region_name"] == "D区")
check("D 区以操作下标 0 标识（region_id 为 null），穿越 10:00:30~10:01:30",
      res_d["region_id"] is None and res_d["operation_index"] == 0
      and res_d["crossings"][0]["entry_time"] == "2030-06-01T10:00:30Z"
      and res_d["crossings"][0]["exit_time"] == "2030-06-01T10:01:30Z", str(res_d))
res_e = next(x for x in t0["results"] if x["region_name"] == "E区")
check("E 区以操作下标 1 标识，穿越 10:04:30~10:05:30",
      res_e["region_id"] is None and res_e["operation_index"] == 1
      and res_e["crossings"][0]["entry_time"] == "2030-06-01T10:04:30Z"
      and res_e["crossings"][0]["exit_time"] == "2030-06-01T10:05:30Z", str(res_e))
check("现有区域（按编号升序）排在拟登记区域之前",
      [x["region_id"] for x in t0["results"][:2]] == [A, B]
      and all(x["region_id"] is None for x in t0["results"][2:]), str(t0["results"]))

# ====================================================================
# 3. 删除挡路区域后轨迹安全
# ====================================================================
r = preview(2, [{"action": "delete", "id": A}, {"action": "delete", "id": B}], [T_CROSS])
p = r.json()
check("删除全部区域后 T_CROSS 安全、committable=true",
      p["trajectories"][0]["safe"] is True and p["committable"] is True, str(p))
check("删除后集合为空：results 为空列表", p["trajectories"][0]["results"] == [], str(p))
check("delete 操作回显删除前 version=1",
      p["operations"] == [
          {"index": 0, "action": "delete", "region_id": A, "version": 1},
          {"index": 1, "action": "delete", "region_id": B, "version": 1},
      ], str(p["operations"]))

# ====================================================================
# 4. 错误约定与正式批量变更一致
# ====================================================================
r = preview(1, [{"action": "update", "id": A, "name": "x"}], [T_HIGH])
check("过期 revision → 409 revision_conflict 且带当前值",
      r.status_code == 409
      and r.json()["error"]["code"] == "revision_conflict"
      and r.json()["error"]["details"][0]["current_revision"] == 2, r.text)

r = preview(2, [{"action": "update", "id": A, "name": "x"},
                {"action": "delete", "id": A}], [T_HIGH])
check("同一区域出现两次 → 422 duplicate_region",
      r.status_code == 422 and r.json()["error"]["code"] == "duplicate_region", r.text)

r = preview(2, [{"action": "delete", "id": 999999}], [T_HIGH])
check("未知区域 → 404 region_not_found 且带操作下标与 id",
      r.status_code == 404
      and r.json()["error"]["code"] == "region_not_found"
      and r.json()["error"]["details"][0]["operation_index"] == 0
      and r.json()["error"]["details"][0]["region_id"] == 999999, r.text)

r = preview(2, [{"action": "update", "id": A,
                 "valid_from": iso(at(2, 0)), "valid_to": iso(at(1, 0))}], [T_HIGH])
check("修订后时段倒序 → 422 invalid_operation 且带操作下标",
      r.status_code == 422
      and r.json()["error"]["code"] == "invalid_operation"
      and r.json()["error"]["details"][0]["operation_index"] == 0, r.text)

r = preview(2, [{"action": "create", "name": "坏区", "polygon": SQUARE,
                 "valid_from": iso(at(2, 0)), "valid_to": iso(at(1, 0))}], [T_HIGH])
check("拟登记时段倒序 → 422 invalid_operation",
      r.status_code == 422 and r.json()["error"]["code"] == "invalid_operation", r.text)

r = preview(2, [{"action": "update", "id": A, "name": "x"}],
            [[{"x": 0, "y": 0, "t": "2030-06-01T10:01:00Z"},
              {"x": 1, "y": 1, "t": "2030-06-01T10:00:00Z"}]])
check("轨迹时刻逆序 → 422 invalid_trajectory",
      r.status_code == 422 and r.json()["error"]["code"] == "invalid_trajectory", r.text)

r = preview(2, [{"action": "update", "id": A, "name": "x"}],
            [[{"x": 0, "y": 0, "t": "2030-06-01T10:00:00Z"}]])
check("轨迹点少于 2 个 → 422 validation_error",
      r.status_code == 422 and r.json()["error"]["code"] == "validation_error", r.text)

r = preview(2, [], [T_HIGH])
check("空操作列表 → 422 validation_error",
      r.status_code == 422 and r.json()["error"]["code"] == "validation_error", r.text)

r = preview(2, [{"action": "update", "id": A, "name": "x"}], [])
check("空轨迹列表 → 422 validation_error",
      r.status_code == 422 and r.json()["error"]["code"] == "validation_error", r.text)

# ====================================================================
# 5. 有效通行凭证：若提交将冲突的凭证列入报告
# ====================================================================
r = client.post("/api/passes", json={"points": [
    {"x": -50, "y": 200, "t": "2030-06-01T12:00:00Z"},
    {"x": 950, "y": 200, "t": "2030-06-01T12:10:00Z"},
]})
check("签发凭证 201", r.status_code == 201, r.text)
PASS1 = r.json()["id"]

create_d = [{"action": "create", "name": "D区", "polygon": BOX_D,
             "valid_from": iso(at(1, 0)), "valid_to": iso(at(2, 0))}]
r = preview(2, create_d, [T_CLEAR])
p = r.json()
check("请求轨迹安全但凭证被拟登记 D 拦截 → conflicting_passes 列出凭证",
      p["trajectories"][0]["safe"] is True
      and p["conflicting_passes"] == [{"pass_id": PASS1}], str(p))
check("凭证冲突 → committable=false", p["committable"] is False, str(p))

r = client.post(f"/api/passes/{PASS1}/cancel")
check("取消凭证 200", r.status_code == 200, r.text)
r = preview(2, create_d, [T_CLEAR])
p = r.json()
check("凭证取消后不再冲突 → committable=true",
      p["conflicting_passes"] == [] and p["committable"] is True, str(p))

# ====================================================================
# 6. 择时承诺：变更前仍有未来安全时刻、变更后被清空才列入报告
# ====================================================================
r = client.post("/api/commitments", json={
    "points": [{"x": -50, "y": 200, "t": 0}, {"x": 150, "y": 200, "t": 60}],
    "window_start": "2030-06-01T12:00:00Z",
    "window_end": "2030-06-01T13:00:00Z",
})
check("签发承诺 201", r.status_code == 201, r.text)
COM1 = r.json()["id"]

r = preview(2, create_d, [T_CLEAR])
p = r.json()
check("拟登记 D 清空承诺全部未来安全时刻 → conflicting_commitments 列出承诺",
      p["conflicting_commitments"] == [{"commitment_id": COM1}], str(p))
check("承诺冲突 → committable=false", p["committable"] is False, str(p))

# 只挡住窗口一部分（禁 [12:30,13:00)）：仍有未来安全时刻 → 不冲突
create_d3 = [{"action": "create", "name": "D3区", "polygon": BOX_D,
              "valid_from": "2030-06-01T12:30:45Z",
              "valid_to": "2030-06-01T13:00:15Z"}]
r = preview(2, create_d3, [T_CLEAR])
p = r.json()
check("只挡住窗口一部分 → 承诺不冲突、committable=true",
      p["conflicting_commitments"] == [] and p["committable"] is True, str(p))

r = client.post(f"/api/commitments/{COM1}/cancel")
check("取消承诺 200", r.status_code == 200, r.text)
r = preview(2, create_d, [T_CLEAR])
check("承诺取消后不再冲突", r.json()["conflicting_commitments"] == [], r.text)

# ====================================================================
# 7. 有效观察的结论净翻转（旧/新结论）
# ====================================================================
r = client.post("/api/observations", json={"points": T_CROSS})
check("登记观察 O1（穿 A → unsafe）201",
      r.status_code == 201 and r.json()["conclusion"] == "unsafe", r.text)
O1 = r.json()["id"]
r = client.post("/api/observations", json={"points": [
    {"x": -50, "y": 400, "t": "2030-06-01T10:00:00Z"},
    {"x": 150, "y": 400, "t": "2030-06-01T10:01:00Z"},
]})
check("登记观察 O2（高空 → safe）201",
      r.status_code == 201 and r.json()["conclusion"] == "safe", r.text)
O2 = r.json()["id"]

r = preview(2, [{"action": "delete", "id": A}], [T_CLEAR])
p = r.json()
check("删除 A → O1 净翻转 unsafe→safe，O2 不翻转",
      p["observation_flips"] == [
          {"observation_id": O1, "old_conclusion": "unsafe", "new_conclusion": "safe"}
      ], str(p))

r = preview(2, [{"action": "update", "id": A,
                 "polygon": [[0, 0], [100, 0], [100, 500], [0, 500]]}], [T_CLEAR])
p = r.json()
check("扩大 A → O2 净翻转 safe→unsafe，O1 仍 unsafe 不重复列出",
      p["observation_flips"] == [
          {"observation_id": O2, "old_conclusion": "safe", "new_conclusion": "unsafe"}
      ], str(p))

r = client.post(f"/api/observations/{O2}/cancel")
check("取消 O2 200", r.status_code == 200, r.text)
r = preview(2, [{"action": "update", "id": A,
                 "polygon": [[0, 0], [100, 0], [100, 500], [0, 500]]}], [T_CLEAR])
check("已取消观察不再参与翻转", r.json()["observation_flips"] == [], r.text)

# ====================================================================
# 8. 预演无副作用：revision/区域/审计/凭证/承诺/观察/事件/幂等键均不变
# ====================================================================
check("多轮预演后 revision 仍为 2",
      client.get("/api/revision").json()["revision"] == 2)
regions = client.get("/api/regions").json()
check("区域集合不变（2 个区域，version 均为 1，名称未改）",
      len(regions) == 2 and all(x["version"] == 1 for x in regions)
      and regions[0]["name"] == "A区", str(regions))
check("审计记录不变（恰 2 条登记记录）", table_count("region_change_audit") == 2)
check("观察事件未被预演写入", table_count("observation_events") == 0)
check("幂等键表无占用", table_count("idempotency_keys") == 0)
check("O1 结论未被预演改动（仍 unsafe，revision 仍 2）",
      client.get(f"/api/observations/{O1}").json()["conclusion"] == "unsafe"
      and client.get(f"/api/observations/{O1}").json()["revision"] == 2)

# ====================================================================
# 9. 预演不占用区域编号：正式登记仍拿到下一个 id
# ====================================================================
r = create_region("远区", [[1000, 1000], [1100, 1000], [1100, 1100], [1000, 1100]],
                  at(1, 0), at(3, 0))
check("正式登记 201，id=3（预演未占用编号），revision=3",
      r.status_code == 201 and r.json()["region"]["id"] == 3
      and r.json()["revision"] == 3, r.text)

# ====================================================================
# 10. 预演与正式提交口径一致：同一批内容，预演可提交 → 正式提交成功；
#     正式提交后旧 revision 的预演按过期处理
# ====================================================================
ops10 = [
    {"action": "create", "name": "F区",
     "polygon": [[400, 0], [500, 0], [500, 100], [400, 100]],
     "valid_from": iso(at(1, 0)), "valid_to": iso(at(2, 0))},
    {"action": "update", "id": B, "name": "B区-改名"},
]
r = preview(3, ops10, [T_CLEAR])
p = r.json()
check("预演：committable=true，create 以操作下标标识（region_id=null）",
      p["committable"] is True
      and p["operations"][0]["action"] == "create"
      and p["operations"][0]["region_id"] is None, str(p))

r = client.post("/api/regions/batch", json={
    "expected_revision": 3, "operations": ops10, "trajectories": [T_CLEAR],
})
check("正式提交同一批 200，revision=4，create 分配到正式编号 4",
      r.status_code == 200 and r.json()["revision"] == 4
      and r.json()["operations"][0]["region_id"] == 4
      and r.json()["trajectories"][0]["safe"] is True, r.text)

r = preview(3, ops10, [T_CLEAR])
check("正式提交后旧 revision 预演 → 409 revision_conflict",
      r.status_code == 409 and r.json()["error"]["code"] == "revision_conflict", r.text)

reset_all()
print()
if failures:
    print(f"失败 {len(failures)} 项：{failures}")
    sys.exit(1)
print("全部批量变更影响预演 API 集成用例通过")
