# nav_context.py
# 길안내 진행 상태(nav_state) — 프런트가 주기적으로 보내는 안내 진행 정보를
# 세션에 유지하고, 이동 중 질문("지금 어디로 가", "다음에 뭐", "왜 돌아가")에
# 모델이 답할 근거를 만든다. (#248)
#
# live_bridge 는 연결마다 nav dict 하나를 만들어 이 모듈의 순수 함수로만 다룬다.
# google.genai 등 무거운 의존이 없어 단독 테스트가 가능하다.

# 프런트 nav_state 메시지에서 받아들이는 필드 — 이 외의 키는 버린다.
_NAV_FIELDS = ("route_id", "guiding", "step_idx", "total_steps",
               "current", "next", "remaining_m", "dest_name", "profile",
               "leg_kind",   # walk | bus | subway — 현재 진행 구간의 이동 수단 (#251)
               # 현재(또는 다음) 버스 구간의 승차 정류장·노선 — 실시간 도착정보 조회 키 (v1.39.0)
               "board_station_id", "board_route_id", "board_stop_name",
               # 지하철 하차역 역 안/밖 안내 (#298) — {station, exit_no, where, inside[], outside}
               "station_egress",
               # 지금 또는 곧 탈 역의 승차 전 역 안 이동 (v2.29.6) — {station, inside[], car}
               "station_ingress",
               # 화면이 지금 역 안/밖·출구 확인을 기다리는지 (v1.52.0) — {kind, station, choices[]}
               "station_wait",
               # 이용자가 말로 안내를 멈춘 상태인지 (v2.28.0) — 안내 상태(guiding)는 유지된다
               "paused")


def update_nav_state(nav: dict, msg: dict) -> None:
    """프런트 nav_state 메시지를 세션 상태로 반영한다.

    guiding=false(안내 종료·경로만 표시)도 그대로 저장한다 —
    '방금 안내가 끝났다'는 것 자체가 답변 근거가 되기 때문.
    """
    nav.pop("switching", None)   # 화면이 실제 상태를 알려 왔다 — 넘어가는 중 표시는 끝 (v2.26.0)
    for k in _NAV_FIELDS:
        nav[k] = msg.get(k)
    nav["guiding"] = bool(nav.get("guiding"))
    nav["paused"] = bool(nav.get("paused"))
    nav["station_egress"] = _clean_egress(nav.get("station_egress"))
    nav["station_ingress"] = _clean_ingress(nav.get("station_ingress"))
    nav["station_wait"] = _clean_wait(nav.get("station_wait"))
    # step_idx/total_steps/remaining_m 은 정수화 실패 시 None 으로 방어
    for k in ("step_idx", "total_steps", "remaining_m"):
        v = nav.get(k)
        if v is None:
            continue
        try:
            nav[k] = int(v)
        except (TypeError, ValueError):
            nav[k] = None


def _clean_egress(v):
    """프런트가 보낸 역 안/밖 안내를 허용 필드·길이로 정리한다(모델 입력에 그대로 들어가므로)."""
    if not isinstance(v, dict):
        return None
    where = v.get("where") if v.get("where") in ("inside", "outside") else None
    inside = [str(x)[:160] for x in (v.get("inside") or []) if x][:4]
    out = {
        "station": str(v.get("station") or "")[:20] or None,
        "exit_no": str(v.get("exit_no") or "")[:6] or None,
        "where": where,
        "inside": inside,
        "outside": str(v.get("outside") or "")[:200] or None,
    }
    return out if (out["station"] and (inside or out["outside"])) else None


def _clean_ingress(v):
    """프런트가 보낸 승차 전 역 안 이동(경로 서비스 ingress)을 허용 필드·길이로 정리한다 (v2.29.6)."""
    if not isinstance(v, dict):
        return None
    inside = [str(x)[:160] for x in (v.get("inside") or []) if x][:4]
    out = {"station": str(v.get("station") or "")[:20] or None, "inside": inside,
           "car": str(v.get("car") or "")[:80] or None}
    return out if (out["station"] and inside) else None


def _st_key(s) -> str:
    s = "".join(str(s or "").split())
    return s[:-1] if s.endswith("역") and len(s) > 1 else s


def route_elevator(nav: dict, station: str = ""):
    """안내 중인 경로에서 이용할 승강기 하나 (v2.29.6) — "엘리베이터 어디야"에는 이것만 답한다.

    타는 역은 승차 승강장 승강기(ingress 의 '승강장' 문장), 내리는 역은 내린 승강장의 승강기(egress 첫 문장).
    역을 말하지 않았으면 타는 역을 먼저 본다. 경로에 없는 역이면 None.
    반환: {"side": "board"|"alight", "station", "text", "car"?}
    """
    nav = nav or {}
    want = _st_key(station)
    ing, eg = nav.get("station_ingress"), nav.get("station_egress")
    if ing and (not want or _st_key(ing.get("station")) == want):
        lines = ing.get("inside") or []
        plat = [x for x in lines if "승강장" in x] or lines
        return {"side": "board", "station": ing.get("station"), "text": plat[0], "car": ing.get("car")}
    if eg and (not want or _st_key(eg.get("station")) == want) and (eg.get("inside") or []):
        return {"side": "alight", "station": eg.get("station"), "text": eg["inside"][0]}
    return None


# 화면이 기다리는 역 안/밖 확인의 종류 (v1.52.0)
#   ask_station — 안내 시작 때 "역 안(승강장)인가요, 역 밖인가요?" (+ 타고 온 방향)
#   alight_ask  — 지하철 하차 스텝의 "역 안인가요, 나오셨나요?"
#   exit        — 역 안 안내 중, 출구로 나오기를 기다린다
#   undo        — 위치로 역 밖이라고 판단해 도보 안내로 넘어간 직후(되돌리기 가능)
_WAIT_KINDS = ("ask_station", "alight_ask", "exit", "undo")


def _clean_wait(v):
    if not isinstance(v, dict) or v.get("kind") not in _WAIT_KINDS:
        return None
    choices = []
    for c in (v.get("choices") or [])[:3]:
        if isinstance(c, dict) and c.get("travel") in ("north", "south"):
            choices.append({"travel": c["travel"], "label": str(c.get("label") or "")[:40]})
    return {"kind": v["kind"], "station": str(v.get("station") or "")[:20] or None, "choices": choices}


def note_new_route(nav: dict, route_id: str, continues: bool = False) -> None:
    """대화(plan_accessible_route)로 새 경로가 만들어진 직후 호출.

    프런트가 새 경로를 받아 nav_state 를 보내기 전의 공백 동안, 이전 경로의
    route_id·step_idx 가 explain_route_segment 에 주입되는 것을 막는다(리뷰 #3).

    continues=True 는 안내 중에 받은 새 경로다(v2.26.0) — 화면이 새 경로로 안내를 이어 가는 중이므로,
    그 공백에 "안내 중이 아니다 / 안내를 시작할까요"라고 답하지 않도록 switching 표시를 남긴다.
    표시는 화면의 다음 nav_state 가 오면 지워진다(update_nav_state).
    """
    if not route_id:
        return
    if nav.get("route_id") != route_id:
        nav.clear()
        nav.update({"route_id": route_id, "guiding": False})
    if continues:
        nav["switching"] = True


def annotate_route_success(result, nav: dict):
    """경로 생성 성공 결과에 '안내 시작을 어떻게 이어갈지'를 덧붙인다 (v2.26.0).

    반드시 note_new_route 보다 먼저 부른다(그 함수가 안내 상태를 비운다).
    - 안내가 진행 중이었다: 화면은 안내를 끄지 않고 새 경로로 곧바로 이어 간다. 그러니 상담원이
      "안내 시작을 눌러 달라"고 말하면 화면과 어긋난다 — 이어진다는 사실만 알리게 한다.
    - 안내 전이다: 버튼을 누르라고 하지 않고 말로 시작할지 한 번 묻게 한다. 손을 쓰기 어려운
      이용자는 버튼을 누르지 못해 안내 없이 이동했다(현장 실증 2026-10-22, "길안내가 진행 중이
      아니에요" 응답 약 10회).
    """
    if not isinstance(result, dict) or result.get("status") != "success":
        return result
    if nav and nav.get("guiding"):
        extra = (" 길안내가 진행 중이었으므로 화면은 새 경로로 안내를 곧바로 이어 갑니다. "
                 "경로 요약(거리·시간·주의 사항)은 그대로 말하되, '안내 시작'을 묻거나 버튼을 눌러 "
                 "달라고 하지 말고 끝에 새 경로로 안내를 이어 간다고 한 문장을 덧붙이세요.")
        result["guidance_continues"] = True
    else:
        extra = (" 요약 끝에 '안내를 시작할까요?'라고 한 번만 여쭤 보세요. 그렇다고 답하면 "
                 "start_guidance 를 호출합니다. 버튼을 눌러 달라고 요구하지 마세요.")
    result["ai_instruction"] = (result.get("ai_instruction") or "") + extra
    return result


# 화면이 말없이 처리하는 도구(안내 시작·멈춤·지도 크기)의 공통 지시 (v2.28.0).
# 상담원이 "네, 안내를 시작할게요"라고 답하면 막 시작한 안내 음성이 끊겼다가 이어졌다(실기기 2026-11-12).
# 화면은 이 도구의 결과가 온 턴의 상담원 음성을 내지 않는다(dropTurn) — 지시는 그 보조다.
SILENT_INSTRUCTION = ("화면이 바로 처리했습니다. 이 결과에 대해서는 아무 말도 하지 말고 턴을 끝내세요. "
                      "확인 문장·인사·설명을 덧붙이지 마세요.")


def start_guidance_result(nav: dict) -> dict:
    """start_guidance 도구 응답 — "안내 시작해"를 말로 받는다 (v2.26.0).

    화면에 경로가 표시돼 있고 아직 안내 전이면 화면에 안내 시작을 지시한다(ui_action).
    경로가 없거나 이미 안내 중이면 화면을 건드리지 않고 그 사실만 답하게 한다.
    세션 상태(nav)만 읽으므로 get_current_guidance 처럼 디스패처를 거치지 않는다.
    """
    nav = nav or {}
    if nav.get("guiding") and nav.get("paused"):
        # 말로 멈춘 안내를 다시 듣는다 (v2.28.0) — 처음부터가 아니라 지금 단계부터
        return {
            "status": "success", "tool_name": "start_guidance", "resumed": True,
            "route_id": nav.get("route_id"),
            "ui_action": {"action": "start_guidance"},
            "ai_instruction": SILENT_INSTRUCTION,
        }
    if nav.get("guiding") or nav.get("switching"):   # switching: 새 경로로 안내를 이어 가는 중
        return {
            "status": "already_guiding", "tool_name": "start_guidance",
            "destination": nav.get("dest_name"),
            "ai_instruction": ("이미 길안내가 진행 중입니다. 그렇다고 한 문장으로만 알리세요. "
                               "지금 안내 내용을 물으면 get_current_guidance 로 확인하세요."),
        }
    if not nav.get("route_id") or _arrived(nav):
        return {
            "status": "no_route", "tool_name": "start_guidance",
            "ai_instruction": ("아직 화면에 안내할 경로가 없습니다. 어디로 가실지 목적지를 한 문장으로 "
                               "여쭤 보세요. 목적지를 들으면 plan_accessible_route 를 호출합니다."),
        }
    return {
        "status": "success", "tool_name": "start_guidance",
        "route_id": nav.get("route_id"),
        "ui_action": {"action": "start_guidance"},
        # 첫 안내는 화면 음성이 바로 말한다 — 상담원은 말하지 않는다 (v2.28.0)
        "ai_instruction": SILENT_INSTRUCTION,
    }


def pause_guidance_result(nav: dict) -> dict:
    """pause_guidance 도구 응답 — "안내 그만", "안내 멈춰"를 말로 받는다 (v2.28.0).

    안내를 끝내는 것이 아니라 멈춘다(홀드). 경로·진행 단계는 그대로 두고 안내 음성만 멈추며,
    "다시 안내해줘"(start_guidance)로 지금 단계부터 이어서 듣는다. 화면에 '안내를 중단합니다'를 띄우고,
    상담원은 말하지 않는다.
    """
    nav = nav or {}
    if nav.get("guiding") or nav.get("switching"):
        return {"status": "success", "tool_name": "pause_guidance",
                "ui_action": {"action": "pause_guidance"},
                "ai_instruction": SILENT_INSTRUCTION}
    return {"status": "idle", "tool_name": "pause_guidance",
            "ai_instruction": "지금은 진행 중인 길안내가 없다고 한 문장으로만 짧게 알리세요."}


_ZOOM_IN = ("in", "zoom_in", "확대", "크게", "+")
_ZOOM_OUT = ("out", "zoom_out", "축소", "작게", "-")


def map_zoom_result(direction: str = "", mode=None) -> dict:
    """map_zoom 도구 응답 — "지도 확대해줘", "지도 작게"를 말로 받는다 (v2.28.0).

    화면이 지도를 한 단계 키우거나 줄이고 축척을 상태 줄에 보여 준다. 상담원은 말하지 않는다.
    길안내 지도가 없는 정책상담 화면(mode="policy")에서는 화면을 건드리지 않는다.
    """
    d = (direction or "").strip().lower()
    d = "in" if d in _ZOOM_IN else "out" if d in _ZOOM_OUT else ""
    if mode == "policy":
        return {"status": "idle", "tool_name": "map_zoom",
                "ai_instruction": "이 화면에는 지도가 없다고, 이동경로 안내 화면에서 쓸 수 있다고 한 문장으로만 알리세요."}
    if not d:
        return {"status": "idle", "tool_name": "map_zoom",
                "ai_instruction": "지도를 크게 할지 작게 할지 한 문장으로 여쭤 보세요."}
    return {"status": "success", "tool_name": "map_zoom", "direction": d,
            "ui_action": {"action": "map_zoom", "dir": d},
            "ai_instruction": SILENT_INSTRUCTION}


_SHEET_OPEN = ("open", "expand", "열어", "펼쳐", "열기", "펼치기", "크게", "올려")
_SHEET_CLOSE = ("close", "collapse", "닫아", "줄여", "접어", "닫기", "접기", "작게", "내려")


def list_sheet_result(action: str = "", mode=None) -> dict:
    """list_sheet 도구 응답 — "목록 열어줘/펼쳐줘", "목록 닫아줘/줄여줘"를 말로 받는다 (v2.30.2).

    open 은 한 단계 펼치기(접힘 → 기본 → 크게), close 는 접기(손잡이의 '목록 접기'와 같다). 상담원은 말하지 않는다.
    """
    a = (action or "").strip().lower()
    a = "open" if a in _SHEET_OPEN else "close" if a in _SHEET_CLOSE else ""
    if mode == "policy":
        return {"status": "idle", "tool_name": "list_sheet",
                "ai_instruction": "이 화면에는 길안내 목록이 없다고, 이동경로 안내 화면에서 쓸 수 있다고 한 문장으로만 알리세요."}
    if not a:
        return {"status": "idle", "tool_name": "list_sheet",
                "ai_instruction": "목록을 펼칠지 접을지 한 문장으로 여쭤 보세요."}
    return {"status": "success", "tool_name": "list_sheet", "action": a,
            "ui_action": {"action": "list_sheet", "dir": a},
            "ai_instruction": SILENT_INSTRUCTION}


# 경로를 만들지 못한 결과의 status — "실패했지만 서비스는 정상"인 사유들이다.
ROUTE_FAILURE_STATUSES = ("place_not_found", "out_of_service_area",
                          "need_destination", "need_location", "error")


def annotate_route_failure(result, nav: dict):
    """경로 생성 실패 결과에 '진행 중인 안내는 유지된다'는 사실을 덧붙인다.

    새 목적지 안내에 실패해도 이미 진행 중인 안내는 멈추지 않는다(이동 중에 안내를
    끊는 것이 더 위험하다). 그런데 그 사실을 말해 주지 않으면, 이용자에게는
    "안내할 수 없다"는 말과 계속 진행되는 화면·음성 안내가 어긋난 것으로 보인다.
    """
    if not isinstance(result, dict):
        return result
    if result.get("status") not in ROUTE_FAILURE_STATUSES:
        return result
    if not (nav and nav.get("guiding")):
        return result

    dest = nav.get("dest_name") or "이전 목적지"
    result["active_guidance"] = {
        "guiding": True,
        "destination": nav.get("dest_name"),
        "route_id": nav.get("route_id"),
        "step_no": (nav["step_idx"] + 1) if nav.get("step_idx") is not None else None,
        "total_steps": nav.get("total_steps"),
    }
    result["ai_instruction"] = (
        (result.get("ai_instruction") or "")
        + " 그리고 지금 %s(으)로 가는 길안내가 진행 중이며 그대로 계속된다는 점을 "
          "반드시 한 문장으로 덧붙이세요. 안내가 멈췄다고 말하지 마세요. 목적지를 바꾸려면 "
          "다시 말씀해 달라고 안내하세요." % dest
    )
    ui = result.get("ui_action")
    if isinstance(ui, dict):
        ui["guiding_kept"] = True
        ui["active_destination"] = nav.get("dest_name")
    return result


def _arrived(nav: dict) -> bool:
    """마지막 구간까지 간 뒤 안내가 끝났으면 도착으로 본다."""
    si, ts = nav.get("step_idx"), nav.get("total_steps")
    # ts > 1 — 단계가 하나뿐이면 안내 전(step_idx 0)도 마지막 단계라 도착으로 잘못 본다 (v2.26.0)
    return (si is not None and ts is not None and ts > 1 and si >= ts - 1)


def current_guidance_result(nav: dict) -> dict:
    """get_current_guidance 도구 응답 — 세션의 nav_state 를 그대로 읽는다."""
    if not nav or not nav.get("guiding"):
        route_id = (nav or {}).get("route_id")
        if nav and nav.get("switching"):
            # 안내 중에 새 경로를 받아 화면이 넘어가는 중 (v2.26.0) — 안내가 꺼진 것이 아니다
            return {
                "status": "switching",
                "tool_name": "get_current_guidance",
                "route_id": route_id,
                "ai_instruction": ("새 경로로 안내를 이어 가는 중입니다. 곧 화면 음성이 첫 안내를 말한다고 "
                                   "한 문장으로만 알리세요. 안내를 시작할지 묻지 마세요."),
            }
        if nav and _arrived(nav):
            # 도착 직후의 "지금 어디야?" — 다시 안내를 시작하라고 하면 엉뚱하다(리뷰 #5)
            return {
                "status": "arrived",
                "tool_name": "get_current_guidance",
                "route_id": route_id,
                "destination": nav.get("dest_name"),
                "ai_instruction": (
                    "방금 목적지에 도착해 안내가 끝난 상태입니다. 도착했음을 짧게 알리고, "
                    "다른 도움이 필요한지 물으세요. 안내를 다시 시작하라고 권하지 마세요."
                ),
            }
        return {
            "status": "idle",
            "tool_name": "get_current_guidance",
            "route_id": route_id,
            "ai_instruction": (
                "지금은 길안내가 진행 중이 아니라고 짧게 알리세요. "
                + ("직전에 안내한 경로는 남아 있으니, 안내를 시작할지 여쭤 보세요. 그렇다고 답하면 "
                   "start_guidance 를 호출합니다(화면의 '안내 시작' 버튼으로도 됩니다)." if route_id else
                   "원하시면 목적지를 말씀해 주시라고 안내하세요.")
            ),
        }
    step_no = None
    if nav.get("step_idx") is not None:
        step_no = nav["step_idx"] + 1        # 사람 기준 1부터
    if nav.get("paused"):
        return {
            "status": "paused",
            "tool_name": "get_current_guidance",
            "route_id": nav.get("route_id"),
            "destination": nav.get("dest_name"),
            "current_instruction": nav.get("current"),
            "ai_instruction": ("이용자가 말로 안내를 멈춘 상태입니다. 물은 것에만 짧게 답하고, 다시 들으려면 "
                               "'다시 안내해줘'라고 말하면 된다고 한 문장으로 덧붙이세요. 다시 듣겠다고 하면 start_guidance 를 호출합니다."),
        }
    return {
        "status": "guiding",
        "tool_name": "get_current_guidance",
        "route_id": nav.get("route_id"),
        "destination": nav.get("dest_name"),
        "step_no": step_no,
        "total_steps": nav.get("total_steps"),
        "current_instruction": nav.get("current"),
        "next_instruction": nav.get("next"),
        "remaining_m_to_next": nav.get("remaining_m"),
        "leg_kind": nav.get("leg_kind") or "walk",
        "profile": nav.get("profile"),
        "station_egress": nav.get("station_egress"),
        "station_wait": nav.get("station_wait"),
        "ai_instruction": (
            "이동 중 답변입니다 — 1~2문장으로 짧게. current_instruction(지금 할 안내)을 "
            "그대로 다시 들려주듯 답하세요. next_instruction 과 remaining_m_to_next 는 "
            "사용자가 물었을 때만 덧붙입니다. 내부 번호(route_id)는 읽지 마세요."
            + (" 역 승강기·출구를 물으면 station_egress 를 근거로 답하세요 — 역 안이면 inside 를 "
               "순서대로, 이미 역 밖이면 outside 를 말하고, 어느 쪽인지 모르면 먼저 물으세요."
               if nav.get("station_egress") else "")
        ),
    }


HANDOFF_TOOLS = ("plan_accessible_route", "open_navi_screen")


def inject_handoff(fname: str, fargs: dict, session_mode) -> dict:
    """정책상담 세션이면 길안내 도구를 '넘기기'로 돌린다 (v2.0.0).

    세션 종류는 서버가 아는 사실이다 — 모델이 보낸 handoff 값은 믿지 않고 항상 덮어쓴다.
    이동경로 안내 세션(mode == "navi")에서는 종전대로 경로를 만든다.
    """
    if fname in HANDOFF_TOOLS:
        if (session_mode or "") == "navi":
            fargs.pop("handoff", None)
        else:
            fargs["handoff"] = True
    return fargs


LOCATION_TOOLS = ("find_nearby_transit", "find_emergency_support", "find_toilet",
                  "find_accessible_restaurants", "find_culture_sports", "check_building_accessibility",
                  "find_bf_tour_spots")


def needs_current_location(fname: str, fargs: dict, nav: dict) -> bool:
    """이 도구 호출이 현재 위치를 기준점으로 쓰는가 (v2.19.0).

    정책상담 화면은 위치를 계속 보내지 않는다 — 기준 장소를 말하지 않은 '근처' 찾기가 들어왔을 때만
    세션이 화면에 위치를 한 번 물어본다. 장소를 말했거나(place) 안내 중인 승차 정류장이 있으면 묻지 않는다.
    """
    fargs = fargs or {}
    if fname in LOCATION_TOOLS:
        return not fargs.get("place")
    if fname == "get_bus_arrivals":
        return not (fargs.get("station_id") or fargs.get("place") or (nav or {}).get("board_station_id"))
    return False


def inject_nav_defaults(fname: str, fargs: dict, nav: dict, user_location: dict) -> dict:
    """도구 호출 인자에 세션이 아는 사실을 주입한다 — 모델이 지어내지 못하게.

    - explain_route_segment: 안내가 진행 중이면 route_id·step_idx 기본값을
      '경고 구간 자동 선택'이 아니라 **현재 진행 구간**으로 준다.
      모델이 값을 명시했으면 그대로 둔다(과거 구간 질문 허용).
    - find_nearby_transit / find_emergency_support / find_toilet / find_accessible_restaurants /
      find_culture_sports / check_building_accessibility: 기준 장소(place)를 말하지 않았으면 현재 위치를 쓴다.
    - get_bus_arrivals: 정류장을 말하지 않았으면 안내 중인 버스 구간의 승차 정류장·노선을,
      그것도 없으면 현재 위치(가장 가까운 정류장 탐색)를 쓴다.
    """
    if fname == "explain_route_segment" and nav:
        if not fargs.get("route_id") and nav.get("route_id"):
            fargs["route_id"] = nav["route_id"]
        if (fargs.get("step_idx") is None and nav.get("guiding")
                and nav.get("step_idx") is not None
                and fargs.get("route_id") == nav.get("route_id")):
            fargs["step_idx"] = nav["step_idx"]
    elif fname == "find_bf_tour_spots":
        # 관광지 추천(#298) — 현재 위치가 있으면 같은 충족도 등급 안에서 가까운 순으로 받는다
        if user_location and user_location.get("lat") is not None:
            fargs["origin_lat"] = user_location["lat"]
            fargs["origin_lng"] = user_location["lng"]
        else:
            fargs.pop("origin_lat", None)
            fargs.pop("origin_lng", None)
    elif fname in ("find_nearby_transit", "find_emergency_support", "find_toilet",
                   "find_accessible_restaurants", "find_culture_sports", "check_building_accessibility"):
        # 긴급대응·화장실(#296)도 정류장과 같다 — 기준 장소를 말하지 않았으면 현재 위치
        if not fargs.get("place"):
            if user_location and user_location.get("lat") is not None:
                fargs["lat"] = user_location["lat"]
                fargs["lng"] = user_location["lng"]
            else:
                fargs.pop("lat", None)
                fargs.pop("lng", None)
    elif fname == "get_bus_arrivals":
        if not fargs.get("station_id") and not fargs.get("place"):
            if nav and nav.get("board_station_id"):
                fargs["station_id"] = str(nav["board_station_id"])
                if not fargs.get("route_id") and nav.get("board_route_id"):
                    fargs["route_id"] = str(nav["board_route_id"])
                if nav.get("board_stop_name"):
                    fargs["station_name"] = nav["board_stop_name"]
            elif user_location and user_location.get("lat") is not None:
                fargs["lat"] = user_location["lat"]
                fargs["lng"] = user_location["lng"]
            else:
                fargs.pop("lat", None)
                fargs.pop("lng", None)
    elif fname == "get_weather":
        # 날씨는 시·구 단위면 충분하다 — 위치를 모르면 서버가 안양시 기준으로 답한다 (v2.29.0)
        if not fargs.get("place") and user_location and user_location.get("lat") is not None:
            fargs["lat"] = user_location["lat"]
            fargs["lng"] = user_location["lng"]
        elif not fargs.get("place"):
            fargs.pop("lat", None)
            fargs.pop("lng", None)
    elif fname == "get_station_facilities":
        # 안내 중이면 경로에서 탈(내릴) 승강기를 세션이 준다 — 모델이 고르지 않게 (v2.29.6)
        re_ = route_elevator(nav, fargs.get("station") or "")
        if re_:
            fargs["route_elevator"] = re_
            if not fargs.get("station"):
                fargs["station"] = re_["station"]
        else:
            fargs.pop("route_elevator", None)
    elif fname == "report_station_position":
        # 화면이 무엇을 기다리는지는 세션만 안다 (v1.52.0) — 모델이 지어내지 못하게 덮어쓴다
        fargs["station_wait"] = (nav or {}).get("station_wait")
    elif fname == "report_accessibility_issue":
        # 제보 좌표·안내 세션은 모델이 아니라 세션이 아는 사실로만 채운다 (v1.35.0)
        if user_location and user_location.get("lat") is not None:
            fargs["lat"] = user_location["lat"]
            fargs["lng"] = user_location["lng"]
        else:
            fargs.pop("lat", None)
            fargs.pop("lng", None)
        if nav and nav.get("route_id"):
            fargs["route_id"] = nav["route_id"]
    return fargs
