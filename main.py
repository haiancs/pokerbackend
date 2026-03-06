import os
import sys
import uvicorn
import socketio
import asyncio
from typing import Dict, Optional, Any
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from texasholdem import TexasHoldEm, HandPhase, ActionType, Card, evaluator

# -----------------------------------------------------------------------------
# 1. 基础配置与 SocketIO 初始化
# -----------------------------------------------------------------------------

# 创建 Socket.IO 服务器 (Async)
# cors_allowed_origins='*' 允许跨域
sio = socketio.AsyncServer(async_mode='asgi', cors_allowed_origins='*')

# 创建 FastAPI 应用
app = FastAPI(
    title="Poker Backend (FastAPI + SocketIO)",
    description="Texas Hold'em Poker Backend using texasholdem library",
    version="1.0.0"
)

# 允许 CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 将 Socket.IO 挂载到 FastAPI
# socket_app = socketio.ASGIApp(sio, app) 
# 注意：在 uvicorn 启动时，我们需要直接运行这个 socket_app，或者将 socket_app 挂载到路径
# 推荐方式：使用 socketio.ASGIApp 包装 FastAPI app
app_asgi = socketio.ASGIApp(sio, app)

# -----------------------------------------------------------------------------
# 2. 游戏状态管理 (In-Memory)
# -----------------------------------------------------------------------------

class GameRoom:
    def __init__(self, room_id: str, max_players: int = 9, max_hands: Optional[int] = None):
        self.room_id = room_id
        # 初始化核心引擎
        from texasholdem import PlayerState
        self.engine = TexasHoldEm(
            buyin=1000, # 初始设置一个值
            big_blind=20, 
            small_blind=10, 
            max_players=max_players
        )
        # 立即将所有玩家设为 0 筹码并跳过，等待 join_table
        for p in self.engine.players:
            p.chips = 0
            p.state = PlayerState.SKIP

        # 映射: socket_id -> player_id (int)
        self.sid_to_pid: Dict[str, int] = {}
        # 映射: persistent_id (str) -> player_id (int)
        self.uid_to_pid: Dict[str, int] = {}
        # 映射: player_id -> player_info (name, etc.)
        self.pid_to_info: Dict[int, Dict[str, Any]] = {}
        # 游戏是否正在进行
        self.is_active = False
        self.max_hands = max_hands if max_hands and max_hands > 0 else max_players * 2
        self.hands_played = 0
        self.game_over = False

    def add_player(self, sid: str, name: str, chips: int, uid: str = None):
        """
        添加或重新连接玩家
        uid: 客户端持久化的唯一 ID
        """
        # 1. 检查是否是重连
        if uid and uid in self.uid_to_pid:
            pid = self.uid_to_pid[uid]
            print(f"Player {name} reconnecting with uid {uid} to pid {pid}")
            # 更新 sid 映射
            self.sid_to_pid[sid] = pid
            if pid in self.pid_to_info:
                self.pid_to_info[pid]['sid'] = sid
                self.pid_to_info[pid]['name'] = name # 可能更改了名字
                self.pid_to_info[pid]['online'] = True
            return pid

        # 2. 新玩家加入
        
        # 2.1 检查游戏是否已开始
        if self.is_active:
             raise Exception("游戏已开始，无法加入")

        # 2.2 检查是否已满
        if len(self.uid_to_pid) >= self.engine.max_players:
            raise Exception("Room is full")
            
        # 分配一个 ID (0 to max_players-1)
        used_pids = set(self.uid_to_pid.values())
        new_pid = -1
        for i in range(self.engine.max_players):
            if i not in used_pids:
                new_pid = i
                break
        
        if new_pid == -1:
            raise Exception("No seats available")
            
        self.sid_to_pid[sid] = new_pid
        if uid:
            self.uid_to_pid[uid] = new_pid
            
        self.pid_to_info[new_pid] = {
            "name": name,
            "chips": chips,
            "sid": sid,
            "uid": uid,
            "is_ready": False,
            "online": True
        }
        
        # 关键修复：确保 player 存在且有筹码
        # texasholdem 库初始化时 players 列表可能为空，或者根据 max_players 填充了 None
        # 我们需要检查并初始化
        # 实际上 texasholdem 0.11.0 在初始化时会创建 max_players 个 Player 对象
        
        if new_pid < len(self.engine.players):
             self.engine.players[new_pid].chips = chips
             # 重置状态，确保不是 OUT
             from texasholdem import PlayerState
             self.engine.players[new_pid].state = PlayerState.TO_CALL 

        # 确保其他未坐下的座位状态是 SKIP 或 OUT，以免 start_hand 把他们算进去
        for i in range(self.engine.max_players):
            if i not in self.pid_to_info:
                if i < len(self.engine.players):
                     self.engine.players[i].state = PlayerState.SKIP
        
        return new_pid

    def remove_player(self, sid: str):
        if sid in self.sid_to_pid:
            pid = self.sid_to_pid[sid]
            # 标记为离线，但不立即删除，除非游戏未开始
            if pid in self.pid_to_info:
                self.pid_to_info[pid]['online'] = False
                
            # 如果游戏没在进行，可以直接移除
            if not self.is_active:
                del self.sid_to_pid[sid]
                if pid in self.pid_to_info:
                    uid = self.pid_to_info[pid].get('uid')
                    if uid and uid in self.uid_to_pid:
                        del self.uid_to_pid[uid]
                    del self.pid_to_info[pid]
                return pid
            
            # 游戏进行中，只移除 sid 映射，保留 uid 映射供重连
            del self.sid_to_pid[sid]
            return pid
        return None

    def get_public_state(self):
        """
        构建发送给前端的公共状态对象
        适配前端需要的字段格式
        """
        # 转换社区牌
        community_cards = []
        if self.engine.board:
            for c in self.engine.board:
                community_cards.append(card_to_str(c))
                
        # 构建玩家列表
        players_data = []
        # 按座位顺序遍历
        for i in range(self.engine.max_players):
            info = self.pid_to_info.get(i)
            # 即使座位无人，也可能需要返回占位符？前端可能需要
            if not info:
                continue
                
            # 从引擎获取该玩家的实时筹码和下注信息
            eng_player = None
            if i < len(self.engine.players):
                 eng_player = self.engine.players[i]
            
            # 状态映射
            is_turn = (self.engine.current_player == i and self.engine.is_hand_running())
            print(f"DEBUG: Player {i} isTurn={is_turn} (current_player={self.engine.current_player}, hand_running={self.engine.is_hand_running()})")
            
            # 牌型
            hand = ['XX', 'XX']
            
            # 筹码和下注
            chips = eng_player.chips if eng_player else info['chips']
            
            # 获取当前下注额
            # texasholdem 库中，下注额可能分散在 pots 中
            # player_bet_amount(i) 返回当前轮次的下注
            try:
                bet = self.engine.player_bet_amount(i)
            except:
                bet = 0
            
            status = 'active'
            if eng_player:
                 # Check PlayerState enum
                 state_val = eng_player.state
                 # Need to check what state is. It's an Enum.
                 # Assuming 0=SKIP, 1=TO_CALL, etc.
                 # Let's use string representation if possible or try-catch
                 # The library uses IntEnum: SKIP=0, TO_CALL=1, IN=2, ALL_IN=3, FOLDED=4, OUT=5
                 if state_val.name == 'ALL_IN': status = 'all-in'
                 if state_val.name == 'FOLDED': status = 'fold'
            
            players_data.append({
                "socketId": info['sid'],
                "name": info['name'],
                "online": info.get('online', True),
                "chips": chips,
                "bet": bet,
                "folded": status == 'fold',
                "allIn": status == 'all-in',
                "isTurn": is_turn,
                "isReady": info['is_ready'],
                "hand": hand, 
                "id": i 
            })

        # 安全获取 currentBet
        current_bet = 0
        if self.engine.is_hand_running() and self.engine.current_player is not None and self.engine.current_player >= 0:
             try:
                 # chips_to_call is how much MORE they need to put in
                 to_call = self.engine.chips_to_call(self.engine.current_player)
                 already_bet = self.engine.player_bet_amount(self.engine.current_player)
                 current_bet = to_call + already_bet
                 
                 print(f"DEBUG: current_player={self.engine.current_player}, to_call={to_call}, already_bet={already_bet}, current_bet={current_bet}")
             except:
                 import traceback
                 traceback.print_exc()
                 current_bet = 0

        # 安全获取 pot
        pot = 0
        if self.engine.pots:
            pot = sum(p.get_total_amount() for p in self.engine.pots)

        # 获取赢家信息
        winners = []
        showdown = False
        alive_players = 0
        for i in range(self.engine.max_players):
            info = self.pid_to_info.get(i)
            if not info:
                continue
            if i < len(self.engine.players):
                state_name = self.engine.players[i].state.name
                if state_name not in ('FOLDED', 'OUT', 'SKIP'):
                    alive_players += 1
        if alive_players >= 2 and not self.engine.is_hand_running():
            showdown = True

        from texasholdem import HandPhase
        # 检查是否在 SETTLE 阶段或手牌结束
        # texasholdem 0.11.0: hand_history[HandPhase.SETTLE]
        if self.engine.hand_history and HandPhase.SETTLE in self.engine.hand_history:
            settle_history = self.engine.hand_history[HandPhase.SETTLE]
            # settle_history.pot_winners 是字典 {pot_id: (amount, best_rank, [player_ids])}
            if settle_history and settle_history.pot_winners:
                for pot_id, (amount, rank, pids) in settle_history.pot_winners.items():
                    # 获取牌型描述
                    rank_str = "" # TODO: 使用 evaluator.rank_to_string(rank) 如果可用
                    # 简单转换赢家 ID 为名字
                    for pid in pids:
                        if pid in self.pid_to_info:
                            winners.append({
                                "id": pid,
                                "name": self.pid_to_info[pid]['name'],
                                "amount": amount, # 注意：这是整个底池，如果多人分需要除以 len(pids)
                                "handRank": rank,
                                "handRankText": safe_rank_to_string(rank)
                            })

        min_raise = self.engine.big_blind
        try:
            min_raise = self.engine.min_raise()
        except:
            pass

        state = "GAME_OVER" if self.game_over else (phase_to_str(self.engine.hand_phase) if self.engine.is_hand_running() else "WAITING")

        return {
            "state": state,
            "pot": pot,
            "communityCards": community_cards,
            "currentBet": current_bet,
            "dealerIndex": self.engine.btn_loc,
            "players": players_data,
            "minRaise": min_raise,
            "minTotalRaiseTo": current_bet + min_raise,
            "bigBlind": self.engine.big_blind,
            "handsPlayed": self.hands_played,
            "maxHands": self.max_hands,
            "winners": winners,
            "showdown": showdown
        }

# 全局房间字典
rooms: Dict[str, GameRoom] = {}

# -----------------------------------------------------------------------------
# 3. 辅助函数
# -----------------------------------------------------------------------------

def card_to_str(card: int) -> str:
    """将 texasholdem 的 int 牌转换为前端格式 'Ah', 'Td' 等"""
    # 库的 Card 对象实际上是一个 int
    # 我们可以用 str(Card(card)) 得到 "Ah", "2s" 等格式
    # texasholdem 0.11.0 Card class has __str__ or __repr__
    
    return str(Card(card))

def phase_to_str(phase) -> str:
    if phase == HandPhase.PREFLOP: return "PREFLOP"
    if phase == HandPhase.FLOP: return "FLOP"
    if phase == HandPhase.TURN: return "TURN"
    if phase == HandPhase.RIVER: return "RIVER"
    if phase == HandPhase.SETTLE: return "SHOWDOWN" # 库可能是 SETTLE
    return "WAITING"

def safe_rank_to_string(rank: Any) -> str:
    try:
        rank_int = int(rank)
    except:
        return "未知牌型"
    if rank_int < 1 or rank_int > 7462:
        return "未知牌型"
    try:
        rank_name = evaluator.rank_to_string(rank_int)
    except:
        return "未知牌型"
    rank_map = {
        "Straight Flush": "同花顺",
        "Four of a Kind": "四条",
        "Full House": "葫芦",
        "Flush": "同花",
        "Straight": "顺子",
        "Three of a Kind": "三条",
        "Two Pair": "两对",
        "Pair": "一对",
        "High card": "高牌"
    }
    return rank_map.get(rank_name, rank_name)

# -----------------------------------------------------------------------------
# 4. Socket.IO 事件处理
# -----------------------------------------------------------------------------

@sio.event
async def connect(sid, environ):
    print(f"Client connected: {sid}")

@sio.event
async def disconnect(sid):
    print(f"Client disconnected: {sid}")
    # 查找并移除玩家
    for room_id, room in rooms.items():
        if sid in room.sid_to_pid:
            # 获取 pid 和 uid
            pid = room.sid_to_pid[sid]
            uid = room.pid_to_info[pid].get('uid')
            
            # 移除玩家 (如果游戏未进行则彻底移除，否则标记离线)
            removed_pid = room.remove_player(sid)
            
            if removed_pid is not None:
                # 只有当玩家被真正移除时（游戏未进行），才广播 player_left
                # 如果游戏进行中，remove_player 返回 pid，但 pid_to_info 还在
                
                # 检查是否真的移除了（通过 pid_to_info）
                if removed_pid not in room.pid_to_info:
                    await sio.emit('player_left', {'playerId': sid, 'uid': uid}, room=room_id)
                else:
                    print(f"Player {sid} (pid={removed_pid}) disconnected but kept in game (active)")
                    # 可以选择广播一个 'player_disconnected' 事件
            
            # 广播更新
            await broadcast_game_state(room_id)
            break

@sio.event
async def join_table(sid, data):
    """
    data: { tableId, playerName, maxHands, maxPlayers, uid }
    """
    room_id = data.get('tableId', 'default')
    player_name = data.get('playerName', 'Guest')
    uid = data.get('uid') # 前端生成的唯一 ID
    
    if not uid:
        # 如果没有 uid，使用 sid 作为临时 uid，或者报错
        # 为了兼容性，如果没有 uid，暂且用 sid
        uid = sid
    
    max_players = data.get('maxPlayers', 9)
    max_hands = data.get('maxHands')
    try:
        max_players = int(max_players) if max_players else 9
    except:
        max_players = 9
    max_players = max(2, min(9, max_players))
    try:
        max_hands = int(max_hands) if max_hands else None
    except:
        max_hands = None

    if room_id not in rooms:
        print(f"Creating new room: {room_id}")
        rooms[room_id] = GameRoom(room_id, max_players=max_players, max_hands=max_hands)
    
    room = rooms[room_id]
    
    try:
        # 如果玩家已经在这个房间里（基于 SID），先移除
        if sid in room.sid_to_pid:
            room.remove_player(sid)

        # 添加玩家
        # 初始筹码 1000
        # 注意：add_player 内部会处理重连逻辑（如果 uid 已存在）
        # 如果是重连，chips 参数会被忽略
        pid = room.add_player(sid, player_name, 1000, uid=uid)
        
        await sio.enter_room(sid, room_id)
        
        print(f"Player {player_name} (pid={pid}, uid={uid}) joined room {room_id}")

        # 通知房间
        # 如果是重连，可能不需要发 player_joined，或者发一个 reconnected
        # 这里统一发 joined，前端可以根据 uid 去重或更新
        await sio.emit('player_joined', {'player': {'name': player_name, 'chips': 1000, 'uid': uid}}, room=room_id)
        
        # 广播最新状态
        await broadcast_game_state(room_id)
        
    except Exception as e:
        print(f"Error joining table: {e}")
        import traceback
        traceback.print_exc()
        await sio.emit('error', {'message': str(e)}, to=sid)

@sio.event
async def player_ready(sid, data):
    room_id = data.get('tableId')
    if room_id not in rooms: return
    room = rooms[room_id]
    
    if sid in room.sid_to_pid:
        if room.game_over:
            await broadcast_game_state(room_id)
            return

        pid = room.sid_to_pid[sid]
        info = room.pid_to_info[pid]
        info['is_ready'] = not info['is_ready'] # Toggle
        
        print(f"Player {info['name']} (pid={pid}) ready status: {info['is_ready']}")
        
        # 检查是否所有人都准备好了，且人数 >= 2
        # 注意：这里需要统计有效玩家（筹码>0）的准备情况
        # 简单起见，只要有 >=2 个有效玩家且所有在座玩家（或者所有有效玩家）都准备好了即可
        
        valid_players = []
        for p_id, p_info in room.pid_to_info.items():
            if p_id < len(room.engine.players):
                # 检查筹码
                if room.engine.players[p_id].chips > 0:
                    valid_players.append(p_info)
        
        ready_count = sum(1 for p in valid_players if p['is_ready'])
        total_valid = len(valid_players)
        
        print(f"Room {room_id}: {ready_count}/{total_valid} valid players ready. Active: {room.is_active}")
        
        if total_valid < 2:
            room.game_over = True
            room.is_active = False
            await broadcast_game_state(room_id)
            return

        if not room.is_active and total_valid >= 2 and ready_count == total_valid:
            # Force player state to TO_CALL to ensure they are picked up by start_hand
            from texasholdem import PlayerState
            
            for pid_ in room.pid_to_info:
                if pid_ < len(room.engine.players):
                    # 重置状态，确保 start_hand 能正确处理
                    # 如果筹码为0，设为 OUT
                    if room.engine.players[pid_].chips > 0:
                        room.engine.players[pid_].state = PlayerState.TO_CALL
                    else:
                        room.engine.players[pid_].state = PlayerState.OUT
            
            await start_game(room)
        else:
            await broadcast_game_state(room_id)

@sio.event
async def action(sid, data):
    """
    data: { tableId, action: 'call'|'fold'|'raise'|'check', amount: int }
    """
    room_id = data.get('tableId')
    action_str = data.get('action')
    amount = data.get('amount', 0)
    
    if room_id not in rooms: return
    room = rooms[room_id]
    
    if sid not in room.sid_to_pid: return
    pid = room.sid_to_pid[sid]
    
    # 映射前端 action 到库的 ActionType
    # texasholdem ActionType: CALL, FOLD, RAISE, CHECK, ALL_IN
    # 注意：前端传来的 amount 是“总下注额”还是“加注额”？
    # 通常前端传的是“raise to X” (total amount)
    # texasholdem 库 take_action(action_type, total=X)
    
    try:
        act_type = None
        total = None
        
        if action_str == 'fold':
            act_type = ActionType.FOLD
        elif action_str == 'check':
            # 自动处理需要 Call 的情况，避免 "state ... cannot CHECK" 错误
            if room.engine.chips_to_call(pid) > 0:
                act_type = ActionType.CALL
            else:
                act_type = ActionType.CHECK
        elif action_str == 'call':
            # 自动处理 Check 的情况，避免 "state IN cannot CALL" 错误
            if room.engine.chips_to_call(pid) == 0:
                act_type = ActionType.CHECK
            else:
                act_type = ActionType.CALL
        elif action_str == 'raise':
            act_type = ActionType.RAISE
            total = amount # 需要确认库是接收增量还是总量
            # 库文档：take_action(ActionType.RAISE, total=10) -> raise TO 10
        elif action_str == 'allin':
            act_type = ActionType.ALL_IN
            
        # 执行动作
        if act_type:
            if total is not None:
                room.engine.take_action(act_type, total=total)
            else:
                room.engine.take_action(act_type)
            
            # 广播更新
            await broadcast_game_state(room_id)
            
            # 检查游戏是否结束 (Showdown)
            if not room.engine.is_hand_running():
                # 游戏结束，处理结算
                print(f"Hand ended in room {room_id}")
                room.hands_played += 1
                
                # 1. 广播最终状态（含亮牌）
                await broadcast_game_state(room_id)
                
                # 2. 停止自动流程，重置准备状态
                # 移除 await asyncio.sleep(5)
                
                print(f"Waiting for players to be ready in room {room_id}")
                room.is_active = False # 标记为非活跃，等待 Ready
                
                # 重置所有玩家的 Ready 状态
                for pid, info in room.pid_to_info.items():
                    info['is_ready'] = False

                # 检查是否满足游戏结束条件
                active_with_chips = 0
                for pid_, info_ in room.pid_to_info.items():
                    if pid_ < len(room.engine.players) and room.engine.players[pid_].chips > 0:
                        active_with_chips += 1

                if active_with_chips <= 1:
                    room.game_over = True
                if room.max_hands and room.hands_played >= room.max_hands:
                    room.game_over = True
                    
                # 广播更新（通知前端显示结算 Modal 和准备按钮）
                await broadcast_game_state(room_id)
                
                # 3. 自动开始下一手逻辑已移除，转由 player_ready 事件触发
                
    except Exception as e:
        print(f"Action error: {e}")
        await sio.emit('error', {'message': str(e)}, to=sid)

# -----------------------------------------------------------------------------
# 5. 核心逻辑方法
# -----------------------------------------------------------------------------

async def start_game(room: GameRoom):
    print(f"Starting game in room {room.room_id}")
    if room.game_over:
        await broadcast_game_state(room.room_id)
        return
    room.is_active = True
    room.engine.start_hand()
    await broadcast_game_state(room.room_id)

async def broadcast_game_state(room_id: str):
    if room_id not in rooms: return
    room = rooms[room_id]
    
    public_state = room.get_public_state()
    
    # 获取所有需要发送的 pid 和 sid
    # 避免在迭代时修改字典
    targets = list(room.pid_to_info.items())
    
    reveal_showdown_cards = public_state.get('showdown', False)

    for pid, info in targets:
        sid = info['sid']
        
        # 复制一份状态
        private_state = public_state.copy()
        # 深度复制 players 列表
        private_players = [p.copy() for p in public_state['players']]
        
        if reveal_showdown_cards:
            for p in private_players:
                if p.get('folded'):
                    continue
                hand_ints = room.engine.get_hand(p['id'])
                if hand_ints:
                    p['hand'] = [card_to_str(c) for c in hand_ints]
        else:
            my_hand_ints = room.engine.get_hand(pid)
            if my_hand_ints:
                my_hand_str = [card_to_str(c) for c in my_hand_ints]
                for p in private_players:
                    if p['id'] == pid:
                        p['hand'] = my_hand_str
                        break
        
        private_state['players'] = private_players
        
        await sio.emit('game_update', private_state, to=sid)

# -----------------------------------------------------------------------------
# 6. HTTP 路由 (FastAPI)
# -----------------------------------------------------------------------------

@app.get("/")
async def index():
    return {"message": "Poker Backend Running", "engine": "texasholdem (Python)"}

if __name__ == '__main__':
    # 开发模式启动
    port = int(os.getenv("PORT", 3000))
    uvicorn.run("main:app_asgi", host="0.0.0.0", port=port, reload=True)
