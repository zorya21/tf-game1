from flask import Flask, render_template, request, redirect, url_for, abort, jsonify, session, Response, stream_with_context
import random
import secrets
import string
import os
import json
from contextlib import contextmanager
from datetime import datetime

import redis
from redis.exceptions import LockError, RedisError

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev_secret_key")

CARDS_PER_PLAYER = 24

CARD_POOL = [
    {"name": "王俊凯", "image": "wjk.jpg"},
    {"name": "王源", "image": "wy.jpg"},
    {"name": "易烊千玺", "image": "yyqx.jpg"},
    {"name": "马嘉祺", "image": "mjq.jpg"},
    {"name": "丁程鑫", "image": "dcx.jpg"},
    {"name": "宋亚轩", "image": "syx.jpg"},
    {"name": "刘耀文", "image": "lyw.jpg"},
    {"name": "张真源", "image": "zzy.jpg"},
    {"name": "严浩翔", "image": "yhx.jpg"},
    {"name": "贺峻霖", "image": "hjl.jpg"},
    {"name": "朱志鑫", "image": "22x.jpg"},
    {"name": "张泽禹", "image": "zack.jpg"},
    {"name": "张极", "image": "jeremy.jpg"},
    {"name": "左航", "image": "left.jpg"},
    {"name": "苏新皓", "image": "su.jpg"},
    {"name": "童禹坤", "image": "tyk.jpg"},
    {"name": "邓佳鑫", "image": "djx.jpg"},
    {"name": "穆祉丞", "image": "mzc.jpg"},
    {"name": "张子墨", "image": "zzm.jpg"},
    {"name": "黄朔", "image": "hs.jpg"},
    {"name": "余宇涵", "image": "yyh.jpg"},
    {"name": "张峻豪", "image": "zjh.jpg"},
    {"name": "官俊臣", "image": "gjc.jpg"},
    {"name": "张桂源", "image": "zgy.jpg"},
    {"name": "张函瑞", "image": "zhr.jpg"},
    {"name": "王橹杰", "image": "wlj.jpg"},
    {"name": "左奇函", "image": "zqh.jpg"},
    {"name": "陈奕恒", "image": "cyh.jpg"},
    {"name": "杨博文", "image": "ybw.jpg"},
    {"name": "杨涵博", "image": "yhb.jpg"},
    {"name": "张奕然", "image": "zyr.jpg"},
    {"name": "聂玮辰", "image": "nwc.jpg"},
    {"name": "陈思罕", "image": "csh.jpg"},
    {"name": "魏子宸", "image": "wzc.jpg"},
    {"name": "李煜东", "image": "lyd.jpg"},
    {"name": "陈浚铭", "image": "cjm.jpg"},
    {"name": "王烁然", "image": "wsr.jpg"}
]

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
GAME_TTL_SECONDS = int(os.environ.get("GAME_TTL_SECONDS", str(1 * 60 * 60)))

redis_client = redis.from_url(REDIS_URL, decode_responses=True)


def game_key(room_code):
    return f"guess_game:room:{room_code.upper()}"


def room_lock_key(room_code):
    return f"guess_game:lock:{room_code.upper()}"


def chat_channel_key(room_code):
    return f"guess_game:chat:{room_code.upper()}"


def make_chat_payload(game):
    chat_messages = game.get("chat_messages", [])
    return {
        "ok": True,
        "chat_messages": chat_messages,
        # 不要直接用 len(chat_messages) 当版本号：消息超过 100 条被裁剪后，
        # 长度会一直停在 100，前端就判断不出有新消息。
        "chat_version": game.get("chat_version", len(chat_messages)),
    }


def sse_message(event_name, payload):
    return (
        f"event: {event_name}\n"
        f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
    )


def game_exists(room_code):
    return redis_client.exists(game_key(room_code)) == 1


def load_game(room_code):
    raw = redis_client.get(game_key(room_code))
    if raw is None:
        return None
    return json.loads(raw)


def save_game(room_code, game):
    redis_client.set(
        game_key(room_code),
        json.dumps(game, ensure_ascii=False),
        ex=GAME_TTL_SECONDS,
    )


def delete_game(room_code):
    redis_client.delete(game_key(room_code))


def generate_room_code():
    chars = string.ascii_uppercase + string.digits
    while True:
        room_code = "".join(random.choices(chars, k=6))
        if not game_exists(room_code):
            return room_code


def make_game_state():
    if len(CARD_POOL) < CARDS_PER_PLAYER:
        raise ValueError("固定卡池人数不足，至少需要 24 人。")

    p1_board = random.sample(CARD_POOL, CARDS_PER_PLAYER)
    p2_board = random.sample(CARD_POOL, CARDS_PER_PLAYER)

    p1_secret = random.choice(p1_board)
    p2_secret = random.choice(p2_board)

    return {
        "match_id": secrets.token_hex(8),
        "players": {"p1": None, "p2": None},
        "p1": {
            "board": p1_board,
            "secret": p1_secret,
            "eliminated": [],
        },
        "p2": {
            "board": p2_board,
            "secret": p2_secret,
            "eliminated": [],
        },
        "round": 1,
        "phase": "p1_ask",
        "round_guesses": {"p1": None, "p2": None},
        "wrong_guesses": {"p1": 0, "p2": 0},
        "rematch_ready": {"p1": False, "p2": False},
        "questions": [],
        "guesses": [],
        "chat_messages": [],
        "chat_version": 0,
        "winner": None,
        "finish_reason": None,
    }


def reset_game_for_rematch(game):
    old_players = game["players"]
    old_chat_messages = game.get("chat_messages", [])
    old_chat_version = game.get("chat_version", len(old_chat_messages))
    new_game = make_game_state()
    new_game["players"] = old_players
    new_game["chat_messages"] = old_chat_messages
    new_game["chat_version"] = old_chat_version
    game.clear()
    game.update(new_game)


def get_game(room_code):
    room_code = room_code.upper()
    game = load_game(room_code)
    if game is None:
        abort(404)
    return game


@contextmanager
def locked_existing_game(room_code):
    room_code = room_code.upper()
    lock = redis_client.lock(
        room_lock_key(room_code),
        timeout=10,
        blocking_timeout=5,
    )
    acquired = lock.acquire(blocking=True)
    if not acquired:
        abort(503, description="服务器正忙，请稍后再试。")
    try:
        game = load_game(room_code)
        if game is None:
            abort(404)
        yield game
        save_game(room_code, game)
    finally:
        try:
            lock.release()
        except LockError:
            pass


def get_opponent(player):
    if player == "p1":
        return "p2"
    if player == "p2":
        return "p1"
    abort(404)


def is_started(game):
    return game["players"]["p1"] is not None and game["players"]["p2"] is not None


def get_asker_for_phase(phase):
    if phase == "p1_ask":
        return "p1"
    if phase == "p2_ask":
        return "p2"
    return None


def get_answerer_for_phase(phase):
    if phase == "p2_answer":
        return "p2"
    if phase == "p1_answer":
        return "p1"
    return None


def start_next_round(game):
    game["round"] += 1
    game["phase"] = "p1_ask"
    game["round_guesses"] = {"p1": None, "p2": None}


def finish_game(game, winner, finish_reason):
    game["winner"] = winner
    game["finish_reason"] = finish_reason
    game["phase"] = "finished"


def resolve_guess_phase(game):
    p1_guess = game["round_guesses"]["p1"]
    p2_guess = game["round_guesses"]["p2"]

    if p1_guess is None or p2_guess is None:
        return

    p1_correct = p1_guess.get("correct", False)
    p2_correct = p2_guess.get("correct", False)

    p1_reached_limit = game["wrong_guesses"]["p1"] >= 3
    p2_reached_limit = game["wrong_guesses"]["p2"] >= 3

    if p1_correct and p2_correct:
        finish_game(game, "draw", "both_correct")
    elif p1_correct:
        finish_game(game, "p1", "correct_guess")
    elif p2_correct:
        finish_game(game, "p2", "correct_guess")
    elif p1_reached_limit and p2_reached_limit:
        finish_game(game, "draw", "both_max_wrong")
    elif p1_reached_limit:
        finish_game(game, "p2", "max_wrong")
    elif p2_reached_limit:
        finish_game(game, "p1", "max_wrong")
    else:
        start_next_round(game)


def is_current_guess_round_hidden(game):
    if game["winner"] is not None:
        return False
    if game["phase"] != "guess":
        return False
    p1_guess = game["round_guesses"].get("p1")
    p2_guess = game["round_guesses"].get("p2")
    return p1_guess is None or p2_guess is None


def get_public_round_guesses(game):
    public_round_guesses = {}
    for player, guess in game["round_guesses"].items():
        if guess is None:
            public_round_guesses[player] = None
        else:
            public_round_guesses[player] = {"submitted": True}
    return public_round_guesses


def get_public_guesses(game, viewer):
    hide_current_round = is_current_guess_round_hidden(game)
    public_guesses = []
    for guess in game["guesses"]:
        if hide_current_round and guess.get("round") == game["round"]:
            public_guesses.append({
                "round": guess["round"],
                "player": guess["player"],
                "pending": True,
                "is_mine": guess["player"] == viewer,
            })
        else:
            public_guesses.append(guess)
    return public_guesses


def get_public_wrong_guesses(game):
    hide_current_round = is_current_guess_round_hidden(game)
    public_wrong_guesses = {"p1": 0, "p2": 0}
    for guess in game["guesses"]:
        if guess.get("skipped"):
            continue
        if hide_current_round and guess.get("round") == game["round"]:
            continue
        if not guess.get("correct", False):
            public_wrong_guesses[guess["player"]] += 1
    return public_wrong_guesses


def set_player_session(room_code, player, token):
    session[f"player_{room_code}"] = player
    session[f"token_{room_code}"] = token


def get_current_player_from_game(room_code, game):
    room_code = room_code.upper()
    player = session.get(f"player_{room_code}")
    token = session.get(f"token_{room_code}")
    if player not in ["p1", "p2"]:
        return None
    if game is None:
        return None
    if game["players"].get(player) != token:
        return None
    return player


def get_current_player(room_code):
    room_code = room_code.upper()
    game = get_game(room_code)
    player = session.get(f"player_{room_code}")
    token = session.get(f"token_{room_code}")
    if player not in ["p1", "p2"]:
        return None
    if game["players"].get(player) != token:
        return None
    return player


@app.route("/")
def index():
    return render_template("index.html", error=None)


@app.get("/health")
def health():
    try:
        redis_client.ping()
        return jsonify({"ok": True, "redis": "connected"})
    except RedisError as error:
        return jsonify({"ok": False, "redis": "error", "message": str(error)}), 500


@app.post("/create-room")
def create_room():
    create_lock = redis_client.lock(
        "guess_game:lock:create_room",
        timeout=10,
        blocking_timeout=5,
    )
    acquired = create_lock.acquire(blocking=True)
    if not acquired:
        return render_template("index.html", error="服务器正忙，请稍后再试。"), 503
    try:
        room_code = generate_room_code()
        game = make_game_state()
        player_token = secrets.token_urlsafe(16)
        game["players"]["p1"] = player_token
        save_game(room_code, game)
        set_player_session(room_code, "p1", player_token)
    finally:
        try:
            create_lock.release()
        except LockError:
            pass
    return redirect(url_for("game_page", room_code=room_code))


@app.post("/join-room")
def join_room():
    room_code = request.form.get("room_code", "").strip().upper()
    if not room_code:
        return render_template("index.html", error="请输入房间码。")
    if not game_exists(room_code):
        return render_template("index.html", error="找不到这个房间码。")
    with locked_existing_game(room_code) as game:
        existing_player = get_current_player_from_game(room_code, game)
        if existing_player:
            return redirect(url_for("game_page", room_code=room_code))
        if game["players"]["p2"] is not None:
            return render_template("index.html", error="这个房间已经满了。")
        player_token = secrets.token_urlsafe(16)
        game["players"]["p2"] = player_token
        set_player_session(room_code, "p2", player_token)
    return redirect(url_for("game_page", room_code=room_code))


@app.route("/game/<room_code>")
def game_page(room_code):
    room_code = room_code.upper()
    game = get_game(room_code)
    player = get_current_player_from_game(room_code, game)
    if player is None:
        return render_template("index.html", error="你不是这个房间的玩家，请创建或加入房间。")
    opponent = get_opponent(player)
    known_secret = game[opponent]["secret"]
    started = is_started(game)
    return render_template(
        "player.html",
        room_code=room_code,
        player=player,
        opponent=opponent,
        board=game[player]["board"],
        eliminated=set(game[player]["eliminated"]),
        known_secret=known_secret,
        questions=game["questions"],
        guesses=game["guesses"],
        winner=game["winner"],
        finish_reason=game.get("finish_reason"),
        started=started,
        current_round=game["round"],
        phase=game["phase"],
        match_id=game["match_id"],
    )


@app.post("/toggle/<room_code>")
def toggle_card(room_code):
    room_code = room_code.upper()
    with locked_existing_game(room_code) as game:
        player = get_current_player_from_game(room_code, game)
        if player is None:
            return jsonify({"ok": False, "error": "你不是这个房间的玩家。"}), 403
        data = request.get_json(silent=True) or {}
        name = data.get("name", "").strip()
        player_board_names = [card["name"] for card in game[player]["board"]]
        if name not in player_board_names:
            return jsonify({"ok": False, "error": "这个人物不在你的卡池里。"}), 400
        eliminated = set(game[player]["eliminated"])
        if name in eliminated:
            eliminated.remove(name)
        else:
            eliminated.add(name)
        game[player]["eliminated"] = list(eliminated)
        return jsonify({"ok": True, "eliminated": name in eliminated})


@app.get("/api/state/<room_code>")
def api_state(room_code):
    room_code = room_code.upper()
    game = get_game(room_code)
    player = get_current_player_from_game(room_code, game)
    if player is None:
        return jsonify({"ok": False, "error": "你不是这个房间的玩家。"}), 403

    started = is_started(game)
    phase = game["phase"]
    winner = game["winner"]

    # 自适应轮询间隔
    if winner is not None:
        poll_interval_ms = 30000
    elif not started:
        poll_interval_ms = 5000
    else:
        is_my_turn = False
        if phase == "p1_ask":
            is_my_turn = (player == "p1")
        elif phase == "p2_ask":
            is_my_turn = (player == "p2")
        elif phase == "p1_answer":
            is_my_turn = (player == "p1")
        elif phase == "p2_answer":
            is_my_turn = (player == "p2")
        elif phase == "guess":
            my_guess = game["round_guesses"].get(player)
            is_my_turn = (my_guess is None)
        if is_my_turn:
            poll_interval_ms = 1800
        else:
            poll_interval_ms = 8000

    expected_asker = get_asker_for_phase(phase)
    expected_answerer = get_answerer_for_phase(phase)

    can_ask = started and winner is None and expected_asker == player
    can_answer = started and winner is None and expected_answerer == player
    can_guess = started and winner is None and phase == "guess"

    my_secret = None
    if winner is not None:
        my_secret = game[player]["secret"]

    # 聊天版本号：用递增 counter，避免消息超过 100 条后长度不变。
    chat_version = game.get("chat_version", len(game.get("chat_messages", [])))

    return jsonify({
        "ok": True,
        "room_code": room_code,
        "player": player,
        "started": started,
        "match_id": game["match_id"],
        "current_round": game["round"],
        "phase": phase,
        "can_ask": can_ask,
        "can_answer": can_answer,
        "can_guess": can_guess,
        "round_guesses": get_public_round_guesses(game),
        "wrong_guesses": get_public_wrong_guesses(game),
        "rematch_ready": game["rematch_ready"],
        "questions": game["questions"],
        "guesses": get_public_guesses(game, player),
        "winner": winner,
        "finish_reason": game.get("finish_reason"),
        "my_secret": my_secret,
        "poll_interval_ms": poll_interval_ms,
        "chat_version": chat_version,          # 新增聊天版本号
    })


@app.get("/api/chat-messages/<room_code>")
def api_chat_messages(room_code):
    room_code = room_code.upper()
    game = get_game(room_code)
    player = get_current_player_from_game(room_code, game)
    if player is None:
        return jsonify({"ok": False, "error": "你不是这个房间的玩家。"}), 403

    return jsonify(make_chat_payload(game))


@app.get("/api/chat-stream/<room_code>")
def api_chat_stream(room_code):
    """
    聊天推送接口：浏览器用 EventSource 连接。
    有新消息时，api_send_chat 会通过 Redis publish 通知这里，
    这样前端不需要反复轮询 /api/chat-messages。
    """
    room_code = room_code.upper()
    game = get_game(room_code)
    player = get_current_player_from_game(room_code, game)
    if player is None:
        return jsonify({"ok": False, "error": "你不是这个房间的玩家。"}), 403

    @stream_with_context
    def event_stream():
        pubsub = redis_client.pubsub(ignore_subscribe_messages=True)
        pubsub.subscribe(chat_channel_key(room_code))
        try:
            # 浏览器断线后会自动重连。
            yield "retry: 3000\n\n"

            # 首次连接时先推一次当前聊天记录，避免打开聊天框时再请求一次。
            latest_game = load_game(room_code)
            if latest_game is not None:
                yield sse_message("chat", make_chat_payload(latest_game))

            while True:
                message = pubsub.get_message(timeout=20)
                if message and message.get("type") == "message":
                    yield sse_message("chat", json.loads(message["data"]))
                else:
                    # 心跳，防止部署平台长时间无输出而断开连接。
                    yield ": ping\n\n"
        except GeneratorExit:
            pass
        finally:
            pubsub.close()

    return Response(
        event_stream(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/ask/<room_code>")
def api_ask_question(room_code):
    room_code = room_code.upper()
    with locked_existing_game(room_code) as game:
        player = get_current_player_from_game(room_code, game)
        if player is None:
            return jsonify({"ok": False, "error": "你不是这个房间的玩家。"}), 403
        if not is_started(game):
            return jsonify({"ok": False, "error": "等待另一位玩家加入后才能提问。"}), 400
        if game["winner"] is not None:
            return jsonify({"ok": False, "error": "游戏已经结束。"}), 400
        expected_asker = get_asker_for_phase(game["phase"])
        if expected_asker != player:
            return jsonify({"ok": False, "error": "现在还没有轮到你提问。"}), 400
        data = request.get_json(silent=True) or {}
        question = data.get("question", "").strip()
        if not question:
            return jsonify({"ok": False, "error": "问题不能为空。"}), 400
        game["questions"].append({
            "id": secrets.token_hex(4),
            "round": game["round"],
            "from_player": player,
            "text": question,
            "answer": None,
            "answered_by": None,
        })
        if game["phase"] == "p1_ask":
            game["phase"] = "p2_answer"
        elif game["phase"] == "p2_ask":
            game["phase"] = "p1_answer"
        return jsonify({"ok": True})


@app.post("/api/answer/<room_code>/<question_id>")
def api_answer_question(room_code, question_id):
    room_code = room_code.upper()
    with locked_existing_game(room_code) as game:
        player = get_current_player_from_game(room_code, game)
        if player is None:
            return jsonify({"ok": False, "error": "你不是这个房间的玩家。"}), 403
        if not is_started(game):
            return jsonify({"ok": False, "error": "等待另一位玩家加入后才能回答。"}), 400
        if game["winner"] is not None:
            return jsonify({"ok": False, "error": "游戏已经结束。"}), 400
        expected_answerer = get_answerer_for_phase(game["phase"])
        if expected_answerer != player:
            return jsonify({"ok": False, "error": "现在还没有轮到你回答。"}), 400
        data = request.get_json(silent=True) or {}
        answer = data.get("answer")
        if answer not in ["yes", "no"]:
            return jsonify({"ok": False, "error": "回答只能是 yes 或 no。"}), 400
        for q in game["questions"]:
            if q["id"] == question_id:
                if q["from_player"] == player:
                    return jsonify({"ok": False, "error": "不能回答自己提出的问题。"}), 400
                if q["answer"] is not None:
                    return jsonify({"ok": False, "error": "这个问题已经回答过了。"}), 400
                if q.get("round") != game["round"]:
                    return jsonify({"ok": False, "error": "不能回答上一轮的问题。"}), 400
                q["answer"] = "是" if answer == "yes" else "否"
                q["answered_by"] = player
                if game["phase"] == "p2_answer":
                    game["phase"] = "p2_ask"
                elif game["phase"] == "p1_answer":
                    game["phase"] = "guess"
                return jsonify({"ok": True})
        return jsonify({"ok": False, "error": "找不到这个问题。"}), 404


@app.post("/guess/<room_code>")
def guess_secret(room_code):
    room_code = room_code.upper()
    with locked_existing_game(room_code) as game:
        player = get_current_player_from_game(room_code, game)
        if player is None:
            return render_template("index.html", error="你不是这个房间的玩家。")
        if game["winner"] is not None:
            return redirect(url_for("game_page", room_code=room_code))
        if game["phase"] != "guess":
            return redirect(url_for("game_page", room_code=room_code))
        if game["round_guesses"][player] is not None:
            return redirect(url_for("game_page", room_code=room_code))
        guess_name = request.form.get("guess", "").strip()
        player_board_names = [card["name"] for card in game[player]["board"]]
        if guess_name not in player_board_names:
            return redirect(url_for("game_page", room_code=room_code))
        secret_name = game[player]["secret"]["name"]
        correct = guess_name == secret_name
        guess_record = {
            "round": game["round"],
            "player": player,
            "name": guess_name,
            "correct": correct,
            "skipped": False,
        }
        game["guesses"].append(guess_record)
        game["round_guesses"][player] = guess_record
        if not correct:
            game["wrong_guesses"][player] += 1
        resolve_guess_phase(game)
        return redirect(url_for("game_page", room_code=room_code))


@app.post("/api/skip-guess/<room_code>")
def api_skip_guess(room_code):
    room_code = room_code.upper()
    with locked_existing_game(room_code) as game:
        player = get_current_player_from_game(room_code, game)
        if player is None:
            return jsonify({"ok": False, "error": "你不是这个房间的玩家。"}), 403
        if game["winner"] is not None:
            return jsonify({"ok": False, "error": "游戏已经结束。"}), 400
        if game["phase"] != "guess":
            return jsonify({"ok": False, "error": "现在还不能选择不猜。"}), 400
        if game["round_guesses"][player] is not None:
            return jsonify({"ok": False, "error": "你本轮已经做出选择了。"}), 400
        skip_record = {
            "round": game["round"],
            "player": player,
            "name": None,
            "correct": False,
            "skipped": True,
        }
        game["guesses"].append(skip_record)
        game["round_guesses"][player] = skip_record
        resolve_guess_phase(game)
        return jsonify({"ok": True})


@app.post("/api/chat/<room_code>")
def api_send_chat(room_code):
    room_code = room_code.upper()
    chat_record = None
    chat_payload = None

    with locked_existing_game(room_code) as game:
        player = get_current_player_from_game(room_code, game)
        if player is None:
            return jsonify({"ok": False, "error": "你不是这个房间的玩家。"}), 403
        data = request.get_json(silent=True) or {}
        message = data.get("message", "").strip()
        if not message:
            return jsonify({"ok": False, "error": "消息不能为空。"}), 400
        if len(message) > 300:
            return jsonify({"ok": False, "error": "消息太长了，请控制在 300 字以内。"}), 400

        chat_record = {
            "id": secrets.token_hex(4),
            "player": player,
            "text": message,
            "created_at": datetime.now().strftime("%H:%M"),
        }
        game.setdefault("chat_messages", []).append(chat_record)
        game["chat_version"] = game.get("chat_version", len(game["chat_messages"]) - 1) + 1
        if len(game["chat_messages"]) > 100:
            game["chat_messages"] = game["chat_messages"][-100:]
        chat_payload = make_chat_payload(game)

    # 注意：离开 with 后游戏状态已经保存到 Redis，再发布推送更稳。
    redis_client.publish(
        chat_channel_key(room_code),
        json.dumps(chat_payload, ensure_ascii=False),
    )

    return jsonify({
        "ok": True,
        "message": chat_record,
        "chat_messages": chat_payload["chat_messages"],
        "chat_version": chat_payload["chat_version"],
    })


@app.post("/api/rematch/<room_code>")
def api_rematch(room_code):
    room_code = room_code.upper()
    with locked_existing_game(room_code) as game:
        player = get_current_player_from_game(room_code, game)
        if player is None:
            return jsonify({"ok": False, "error": "你不是这个房间的玩家。"}), 403
        if game["winner"] is None or game["phase"] != "finished":
            return jsonify({"ok": False, "error": "只有对局结束后才能再来一局。"}), 400
        game["rematch_ready"][player] = True
        opponent = get_opponent(player)
        if game["rematch_ready"][opponent]:
            reset_game_for_rematch(game)
            return jsonify({
                "ok": True,
                "restarted": True,
                "match_id": game["match_id"],
            })
        return jsonify({
            "ok": True,
            "restarted": False,
            "rematch_ready": game["rematch_ready"],
        })


if __name__ == "__main__":
    app.run(debug=True)