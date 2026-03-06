import asyncio
import socketio
import time

# Configuration
SERVER_URL = "http://localhost:3000"
TABLE_ID = "e2e_table_" + str(int(time.time()))

class PokerClient:
    def __init__(self, name):
        self.name = name
        self.sio = socketio.AsyncClient()
        self.is_ready = False
        self.hand = []
        self.sid = None
        self.chips = 0
        self.my_pid = -1

        # Register callbacks
        self.sio.on('connect', self.on_connect)
        self.sio.on('disconnect', self.on_disconnect)
        self.sio.on('player_joined', self.on_player_joined)
        self.sio.on('game_update', self.on_game_update)
        self.sio.on('error', self.on_error)
        
        self.game_state = None
        self.turn_event = asyncio.Event()

    async def connect(self):
        await self.sio.connect(SERVER_URL)
        self.sid = self.sio.get_sid()
        print(f"[{self.name}] Connected with SID: {self.sid}")

    async def disconnect(self):
        await self.sio.disconnect()

    async def on_connect(self):
        pass

    async def on_disconnect(self):
        print(f"[{self.name}] Disconnected")

    async def on_error(self, data):
        print(f"[{self.name}] ERROR: {data}")

    async def on_player_joined(self, data):
        print(f"[{self.name}] Player joined: {data['player']['name']}")

    async def on_game_update(self, data):
        self.game_state = data
        # Update internal state
        my_player_data = None
        for p in data['players']:
            if p['name'] == self.name:
                self.chips = p['chips']
                self.hand = p.get('hand', [])
                self.is_ready = p['isReady']
                my_player_data = p
                break
        
        state_name = data['state']
        current_player_idx = -1
        
        # Check whose turn it is
        is_my_turn = False
        if my_player_data and my_player_data['isTurn']:
            is_my_turn = True
            self.turn_event.set() # Signal that it's my turn
        else:
            self.turn_event.clear()

        print(f"[{self.name}] Update: State={state_name}, Pot={data['pot']}, MyChips={self.chips}, MyHand={self.hand}, MyTurn={is_my_turn}")

    async def join_table(self):
        print(f"[{self.name}] Joining table {TABLE_ID}...")
        await self.sio.emit('join_table', {
            'tableId': TABLE_ID,
            'playerName': self.name
        })
        # Wait a bit for update
        await asyncio.sleep(0.5)

    async def set_ready(self):
        print(f"[{self.name}] Setting Ready...")
        await self.sio.emit('player_ready', {'tableId': TABLE_ID})
        await asyncio.sleep(0.5)

    async def wait_for_state(self, state_name, timeout=10):
        print(f"[{self.name}] Waiting for state: {state_name}")
        start = time.time()
        while time.time() - start < timeout:
            if self.game_state and self.game_state['state'] == state_name:
                return True
            await asyncio.sleep(0.1)
        return False

    async def act(self, action, amount=0):
        print(f"[{self.name}] Action: {action} (amount={amount})")
        await self.sio.emit('action', {
            'tableId': TABLE_ID,
            'action': action,
            'amount': amount
        })

async def run_test():
    print("=== Starting E2E Poker Test ===")
    
    p1 = PokerClient("Alice")
    p2 = PokerClient("Bob")

    try:
        # 1. Connect
        await p1.connect()
        await p2.connect()

        # 2. Join Table
        await p1.join_table()
        await p2.join_table()

        # 3. Ready Up
        await p1.set_ready()
        await p2.set_ready()

        # 4. Wait for Game Start (PREFLOP)
        success = await p1.wait_for_state("PREFLOP")
        if not success:
            raise Exception("Game did not start (PREFLOP not reached)")
        print(">>> Game Started! PREFLOP")

        # 5. Play Preflop
        # In Heads Up: SB acts first.
        # Find out who is SB (who has turn)
        
        await asyncio.sleep(1) # Wait for turns to settle
        
        players = [p1, p2]
        active_player = None
        
        # Simple loop to play until FLOP
        round_limit = 10
        count = 0
        
        while count < round_limit:
            state = p1.game_state['state']
            if state == 'FLOP':
                print(">>> Reached FLOP!")
                break
            
            # Find who needs to act
            acted = False
            for p in players:
                if p.turn_event.is_set():
                    print(f">>> {p.name}'s turn in {state}")
                    # Simple Strategy: Call or Check
                    # Check current bet
                    current_bet = p.game_state['currentBet']
                    my_bet = 0
                    for pd in p.game_state['players']:
                        if pd['name'] == p.name:
                            my_bet = pd['bet']
                            break
                    
                    to_call = current_bet - my_bet
                    print(f">>> {p.name} needs to call {to_call} (current_bet={current_bet}, my_bet={my_bet})")
                    
                    if to_call > 0:
                        await p.act('call')
                    else:
                        await p.act('check')
                    
                    await asyncio.sleep(2) # Wait for server
                    acted = True
                    break
            
            if not acted:
                await asyncio.sleep(0.5)
            
            count += 1
        
        if p1.game_state['state'] != 'FLOP':
             raise Exception("Failed to reach FLOP")

        # 6. Play Flop (Check-Check)
        print(">>> Playing Flop...")
        count = 0
        while count < round_limit:
            state = p1.game_state['state']
            if state == 'TURN':
                print(">>> Reached TURN!")
                break
            
            acted = False
            for p in players:
                if p.turn_event.is_set():
                    print(f">>> {p.name}'s turn in {state}")
                    await p.act('check')
                    await asyncio.sleep(1)
                    acted = True
                    break
            if not acted: await asyncio.sleep(0.5)
            count += 1

        if p1.game_state['state'] != 'TURN':
             raise Exception("Failed to reach TURN")

        print("=== Test Passed Successfully ===")

    except Exception as e:
        print(f"!!! Test Failed: {e}")
        import traceback
        traceback.print_exc()
    finally:
        await p1.disconnect()
        await p2.disconnect()

if __name__ == "__main__":
    asyncio.run(run_test())
