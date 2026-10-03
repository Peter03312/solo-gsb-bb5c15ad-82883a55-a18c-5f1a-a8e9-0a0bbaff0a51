#!/usr/bin/env bash
# 批量变更影响预演调用示例（POST /api/regions/batch/preview，只读）。
#
# 正式提交多区调整前，用与批量变更相同的请求结构做一次影响预演：
# 报告基于同一已提交快照与同一基准时刻，给出各轨迹在拟变更完整区域集合
# 下的安全结论与穿越明细、若提交将冲突的有效通行凭证、将被清空全部未来
# 安全时刻的择时承诺，以及有效观察的结论净翻转（旧/新结论）。
# 预演不改变区域、凭证、承诺、观察、审计与版本，也不占用区域编号；
# 报告不保证之后可提交，正式提交仍按现有保护规则重验。
#
# 演示：
#   1. 登记 A 区；签发一张 y=200 通行的凭证、一个 y=200 行程的择时承诺、
#      一张直穿 A 区的观察（结论 unsafe）
#   2. 预演「登记 C 区（拦截 y=200）+ 删除 A 区」：
#      报告同时列出凭证冲突、承诺冲突与观察净翻转（unsafe→safe），
#      committable=false；拟登记区域以操作下标标识（region_id 为 null）
#   3. 同一内容正式提交 → 409 pass_conflict（保护规则在正式提交时重验）
#   4. 取消凭证与承诺后重预演 → committable=true；正式提交成功，
#      C 区获得正式编号，revision 只 +1
#   5. 错误契约：过期 revision 409、重复区域 422、未知区域 404、
#      非法操作 422
#   6. 预演无副作用：revision、区域集合与审计历史均不变
#
# 用法：BASE_URL=http://localhost:8000 bash examples/curl_batch_preview.sh
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
J() { python3 -c 'import sys,json; print(json.load(sys.stdin)'"$1"')'; }

preview() {  # $1=expected_revision  $2=operations JSON  $3=trajectories JSON
  curl -fsS -X POST "$BASE_URL/api/regions/batch/preview" \
    -H 'Content-Type: application/json' \
    -d "{\"expected_revision\": $1, \"operations\": $2, \"trajectories\": $3}"
}

# 预演用的待核验轨迹：高空 y=400，不与任何区域接触
TRAJ_CLEAR='[[{"x":-50,"y":400,"t":"2030-06-02T12:00:00Z"},
              {"x":950,"y":400,"t":"2030-06-02T12:10:00Z"}]]'

echo "== 1. 准备：登记 A 区，签发凭证/承诺/观察 =="
R=$(curl -fsS -X POST "$BASE_URL/api/regions" -H 'Content-Type: application/json' \
  -d '{"name":"A区","polygon":[[0,0],[100,0],[100,100],[0,100]],
       "valid_from":"2030-06-01T00:00:00Z","valid_to":"2030-06-03T00:00:00Z"}')
A=$(echo "$R" | J "['region']['id']")
REV=$(echo "$R" | J "['revision']")
echo "A区 id=$A，当前 revision=$REV"

# 凭证：2030-06-02 12:00 沿 y=200 通行（当前无区域拦截）
PASS=$(curl -fsS -X POST "$BASE_URL/api/passes" -H 'Content-Type: application/json' \
  -d '{"points":[{"x":-50,"y":200,"t":"2030-06-02T12:00:00Z"},
                 {"x":950,"y":200,"t":"2030-06-02T12:10:00Z"}]}')
PASS_ID=$(echo "$PASS" | J "['id']")
# 承诺：相对秒数行程 (-50,200)→(150,200)，候选窗口 2030-06-02 10:00~11:00
COM=$(curl -fsS -X POST "$BASE_URL/api/commitments" -H 'Content-Type: application/json' \
  -d '{"points":[{"x":-50,"y":200,"t":0},{"x":150,"y":200,"t":60}],
       "window_start":"2030-06-02T10:00:00Z","window_end":"2030-06-02T11:00:00Z"}')
COM_ID=$(echo "$COM" | J "['id']")
# 观察：直穿 A 区 → 当前结论 unsafe
OBS=$(curl -fsS -X POST "$BASE_URL/api/observations" -H 'Content-Type: application/json' \
  -d '{"points":[{"x":-50,"y":50,"t":"2030-06-02T12:00:00Z"},
                 {"x":150,"y":50,"t":"2030-06-02T12:01:00Z"}]}')
OBS_ID=$(echo "$OBS" | J "['id']")
echo "凭证 id=$PASS_ID，承诺 id=$COM_ID，观察 id=$OBS_ID（结论 $(echo "$OBS" | J "['conclusion']")）"

echo "== 2. 预演「登记 C 区（拦截 y=200）+ 删除 A 区」=="
OPS='[{"action":"create","name":"C区","polygon":[[0,150],[100,150],[100,250],[0,250]],
       "valid_from":"2030-06-02T00:00:00Z","valid_to":"2030-06-03T00:00:00Z"},
      {"action":"delete","id":'"$A"'}]'
preview "$REV" "$OPS" "$TRAJ_CLEAR" | python3 -m json.tool
echo "→ committable=false：凭证 $PASS_ID 与承诺 $COM_ID 将冲突，观察 $OBS_ID 净翻转 unsafe→safe；"
echo "  拟登记 C 区以操作下标 0 标识（region_id 为 null），不占用正式编号"

echo "== 3. 同一内容正式提交 → 仍被保护规则拦下（409 pass_conflict）=="
curl -sS -o /tmp/preview_commit_409.json -w 'HTTP %{http_code}\n' \
  -X POST "$BASE_URL/api/regions/batch" -H 'Content-Type: application/json' \
  -d "{\"expected_revision\": $REV, \"operations\": $OPS, \"trajectories\": $TRAJ_CLEAR}"
python3 -m json.tool /tmp/preview_commit_409.json

echo "== 4. 取消凭证与承诺后重预演 → committable=true；正式提交成功 =="
curl -fsS -X POST "$BASE_URL/api/passes/$PASS_ID/cancel" > /dev/null
curl -fsS -X POST "$BASE_URL/api/commitments/$COM_ID/cancel" > /dev/null
preview "$REV" "$OPS" "$TRAJ_CLEAR" | python3 -m json.tool
BATCH=$(curl -fsS -X POST "$BASE_URL/api/regions/batch" -H 'Content-Type: application/json' \
  -d "{\"expected_revision\": $REV, \"operations\": $OPS, \"trajectories\": $TRAJ_CLEAR}")
echo "$BATCH" | python3 -m json.tool
echo "正式提交：C 区分配到正式编号 $(echo "$BATCH" | J "['operations'][0]['region_id']")，revision=$(echo "$BATCH" | J "['revision']")（整批只 +1）"

echo "== 5. 错误契约（与正式批量变更一致）=="
REV=$(curl -fsS "$BASE_URL/api/revision" | J "['revision']")
echo "-- 5a. 过期 revision → 409 revision_conflict（details 带当前值）--"
curl -sS -o /tmp/preview_409.json -w 'HTTP %{http_code}\n' \
  -X POST "$BASE_URL/api/regions/batch/preview" -H 'Content-Type: application/json' \
  -d "{\"expected_revision\": 0, \"operations\": [{\"action\":\"delete\",\"id\":$A}], \"trajectories\": $TRAJ_CLEAR}"
python3 -m json.tool /tmp/preview_409.json

echo "-- 5b. 同一区域出现两次 → 422 duplicate_region --"
curl -sS -o /tmp/preview_dup.json -w 'HTTP %{http_code}\n' \
  -X POST "$BASE_URL/api/regions/batch/preview" -H 'Content-Type: application/json' \
  -d "{\"expected_revision\": $REV,
       \"operations\": [{\"action\":\"update\",\"id\":$A,\"name\":\"x\"},{\"action\":\"delete\",\"id\":$A}],
       \"trajectories\": $TRAJ_CLEAR}"
python3 -m json.tool /tmp/preview_dup.json

echo "-- 5c. 未知区域 → 404 region_not_found --"
curl -sS -o /tmp/preview_404.json -w 'HTTP %{http_code}\n' \
  -X POST "$BASE_URL/api/regions/batch/preview" -H 'Content-Type: application/json' \
  -d "{\"expected_revision\": $REV, \"operations\": [{\"action\":\"delete\",\"id\":999999}], \"trajectories\": $TRAJ_CLEAR}"
python3 -m json.tool /tmp/preview_404.json

echo "-- 5d. 非法操作（修订后时段倒序）→ 422 invalid_operation --"
curl -sS -o /tmp/preview_badop.json -w 'HTTP %{http_code}\n' \
  -X POST "$BASE_URL/api/regions/batch/preview" -H 'Content-Type: application/json' \
  -d "{\"expected_revision\": $REV,
       \"operations\": [{\"action\":\"update\",\"id\":$A,
                         \"valid_from\":\"2030-06-03T00:00:00Z\",\"valid_to\":\"2030-06-01T00:00:00Z\"}],
       \"trajectories\": $TRAJ_CLEAR}"
python3 -m json.tool /tmp/preview_badop.json

echo "== 6. 预演无副作用：revision、区域集合与审计历史均不变 =="
echo "revision 仍为：$(curl -fsS "$BASE_URL/api/revision" | J "['revision']")"
echo "区域数仍为：$(curl -fsS "$BASE_URL/api/regions" | J ".__len__()")"
echo "审计记录仍为：$(curl -fsS "$BASE_URL/api/regions/changes" | J "['changes'].__len__()") 条"
echo "观察 $OBS_ID 结论仍为：$(curl -fsS "$BASE_URL/api/observations/$OBS_ID" | J "['conclusion']")（预演不写事件、不改结论）"

echo "完成。提示：示例区域会保留，可用 DELETE /api/regions/{id} 清理。"
