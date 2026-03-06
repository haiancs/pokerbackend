from texasholdem import TexasHoldEm, HandPhase, ActionType, PlayerState

def test_heads_up_preflop_repro():
    # 2 Players
    game = TexasHoldEm(buyin=1000, big_blind=20, small_blind=10, max_players=2)
    
    # Simulate setup like main.py
    # 1. Reset all players to SKIP initially
    for p in game.players:
        p.chips = 0
        p.state = PlayerState.SKIP
        
    # 2. Add players (simulate add_player)
    # Player 0
    game.players[0].chips = 1000
    game.players[0].state = PlayerState.TO_CALL
    
    # Player 1
    game.players[1].chips = 1000
    game.players[1].state = PlayerState.TO_CALL
    
    # 3. Before start_hand, force TO_CALL again (like in main.py)
    game.players[0].state = PlayerState.TO_CALL
    game.players[1].state = PlayerState.TO_CALL
    
    # Start Hand
    game.start_hand()
    
    print(f"Hand started. Dealer (Button) index: {game.btn_loc}")
    print(f"SB index: {game.sb_loc}, BB index: {game.bb_loc}")
    
    # Simulate Player SB calling first
    # Check bets before action
    print(f"SB (Player {game.sb_loc}) bet amount: {game.player_bet_amount(game.sb_loc)}")
    print(f"BB (Player {game.bb_loc}) bet amount: {game.player_bet_amount(game.bb_loc)}")
    
    sb_loc = game.sb_loc
    print(f"Player {sb_loc} (SB) Calling...")
    game.take_action(ActionType.CALL)
    
    # Check bets after SB call
    print(f"SB (Player {game.sb_loc}) bet amount after call: {game.player_bet_amount(game.sb_loc)}")
    
    # Now turn should be BB
    bb_loc = game.bb_loc
    print(f"Current player index: {game.current_player} (Should be {bb_loc})")
    
    current_p = game.players[game.current_player]
    print(f"Current player state: {current_p.state}")
    print(f"Chips to call: {game.chips_to_call(game.current_player)}")
    
    # Try to CALL as BB
    try:
        print("Attempting to CALL as BB...")
        game.take_action(ActionType.CALL)
        print("CALL successful")
    except Exception as e:
        print(f"CALL failed: {e}")
        
    # Check state after action
    print(f"After action, current player: {game.current_player}")

if __name__ == "__main__":
    test_heads_up_preflop_repro()
