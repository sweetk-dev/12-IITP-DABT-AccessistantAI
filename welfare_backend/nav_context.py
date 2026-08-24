# nav_context.py
# 길안내 진행 상태(nav_state) — 프런트가 주기적으로 보내는 안내 진행 정보를
# 세션에 유지하고, 이동 중 질문("지금 어디로 가", "다음에 뭐", "왜 돌아가")에
# 모델이 답할 근거를 만든다. (#248)
#
# live_bridge 는 연결마다 nav dict 하나를 만들어 이 모듈의 순수 함수로만 다룬다.
# google.genai 등 무거운 의존이 없어 단독 테스트가 가능하다.

# 프런트 nav_state 메시지에서 받아들이는 필드 — 이 외의 키는 버린다.
_NAV_FIELDS = ("route_id", "guiding", "step_idx", "total_steps",
               "current", "next", "remaining_m", "dest_name", "profile")


def update_nav_state(nav: dict, msg: dict) -> None:
    """프런트 nav_state 메시지를 세션 상태로 반영한다.

    guiding=false(안내 종료·경로만 표시)도 그대로 저장한다 —
    '방금 안내가 끝났다'는 것 자체가 답변 근거가 되기 때문.
    """
    for k in _NAV_FIELDS:
        nav[k] = msg.get(k)
    nav["guiding"] = bool(nav.get("guiding"))
    # step_idx/total_steps/remaining_m 은 정수화 실패 시 None 으로 방어
    for k in ("step_idx", "total_steps", "remaining_m"):
        v = nav.get(k)
        if v is None:
            continue
        try:
            nav[k] = int(v)
        except (TypeError, ValueError):
            nav[k] = None


def note_new_route(nav: dict, route_id: str) -> None:
    """대화(plan_accessible_route)로 새 경로가 만들어진 직후 호출.

    프런트가 새 경로를 받아 nav_state 를 보내기 전의 공백 동안, 이전 경로의
    route_id·step_idx 가 explain_route_segment 에 주입되는 것을 막는다(리뷰 #3).
    """
    if not route_id:
        return
    if nav.get("route_id") != route_id:
        nav.clear()
        nav.update({"route_id": route_id, "guiding": False})


def _arrived(nav: dict) -> bool:
    """마지막 구간까지 간 뒤 안내가 끝났으면 도착으로 본다."""
    si, ts = nav.get("step_idx"), nav.get("total_steps")
    return (si is not None and ts is not None and ts > 0 and si >= ts - 1)


def current_guidance_result(nav: dict) -> dict:
    """get_current_guidance 도구 응답 — 세션의 nav_state 를 그대로 읽는다."""
    if not nav or not nav.get("guiding"):
        route_id = (nav or {}).get("route_id")
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
                + ("직전에 안내한 경로는 남아 있으니, 원하시면 화면의 '안내 시작'으로 "
                   "이어갈 수 있다고 덧붙이세요." if route_id else
                   "원하시면 목적지를 말씀해 주시라고 안내하세요.")
            ),
        }
    step_no = None
    if nav.get("step_idx") is not None:
        step_no = nav["step_idx"] + 1        # 사람 기준 1부터
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
        "profile": nav.get("profile"),
        "ai_instruction": (
            "이동 중 답변입니다 — 1~2문장으로 짧게. current_instruction(지금 할 안내)을 "
            "그대로 다시 들려주듯 답하세요. next_instruction 과 remaining_m_to_next 는 "
            "사용자가 물었을 때만 덧붙입니다. 내부 번호(route_id)는 읽지 마세요."
        ),
    }


def inject_nav_defaults(fname: str, fargs: dict, nav: dict, user_location: dict) -> dict:
    """도구 호출 인자에 세션이 아는 사실을 주입한다 — 모델이 지어내지 못하게.

    - explain_route_segment: 안내가 진행 중이면 route_id·step_idx 기본값을
      '경고 구간 자동 선택'이 아니라 **현재 진행 구간**으로 준다.
      모델이 값을 명시했으면 그대로 둔다(과거 구간 질문 허용).
    - find_nearby_transit: 기준 장소(place)를 말하지 않았으면 현재 위치를 쓴다.
    """
    if fname == "explain_route_segment" and nav:
        if not fargs.get("route_id") and nav.get("route_id"):
            fargs["route_id"] = nav["route_id"]
        if (fargs.get("step_idx") is None and nav.get("guiding")
                and nav.get("step_idx") is not None
                and fargs.get("route_id") == nav.get("route_id")):
            fargs["step_idx"] = nav["step_idx"]
    elif fname == "find_nearby_transit":
        if not fargs.get("place"):
            if user_location and user_location.get("lat") is not None:
                fargs["lat"] = user_location["lat"]
                fargs["lng"] = user_location["lng"]
            else:
                fargs.pop("lat", None)
                fargs.pop("lng", None)
    return fargs
