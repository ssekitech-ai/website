from flask import Flask, render_template, request, jsonify, session
from binance.client import Client
from binance.enums import *
import time
import threading
import os
from pathlib import Path
from datetime import datetime, timedelta

# Load environment variables from .env file if it exists
env_path = Path('.') / '.env'
if env_path.exists():
    with open(env_path) as f:
        for line in f:
            if line.strip() and not line.startswith('#'):
                key, value = line.strip().split('=', 1)
                os.environ.setdefault(key, value)

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', os.urandom(24))

# Initialize Binance Client
api_key = os.environ.get('BINANCE_API_KEY')
api_secret = os.environ.get('BINANCE_API_SECRET')

if not api_key or not api_secret:
    raise ValueError("Binance API credentials not found. Please check your .env file")

client = Client(api_key, api_secret)

# Global variables
stop_balance_display = False
balances = {}
arbitrage_status = {
    "running": False, 
    "current_step": "", 
    "logs": [],
    "active_orders": {},
    "paused": False
}

# Track recently executed opportunities
recently_executed = {}

def get_asset_balance(asset):
    """Get available balance for a specific asset"""
    try:
        balance = client.get_asset_balance(asset=asset)
        return float(balance['free'])
    except Exception as e:
        print(f"Error getting balance for {asset}: {e}")
        return 0.0

def update_balances():
    """Update non-zero balances"""
    global balances
    try:
        account = client.get_account()
        new_balances = {b['asset']: float(b['free']) for b in account['balances'] if float(b['free']) > 0}
        
        # Only update if balances change
        if new_balances != balances:
            balances = new_balances
            return True
    except Exception as e:
        print(f"Error fetching balances: {e}")
    return False

def get_symbol_info(symbol):
    """Get trading rules for a symbol"""
    try:
        info = client.get_symbol_info(symbol)
        if not info:
            raise ValueError(f"Symbol {symbol} not found")
        return info
    except Exception as e:
        print(f"Error getting symbol info: {e}")
        return None

def get_step_size(symbol_info):
    """Get LOT_SIZE step size"""
    for f in symbol_info['filters']:
        if f['filterType'] == 'LOT_SIZE':
            return float(f['stepSize'])
    return 0.000001

def get_tick_size(symbol_info):
    """Get PRICE_FILTER tick size"""
    for f in symbol_info['filters']:
        if f['filterType'] == 'PRICE_FILTER':
            return float(f['tickSize'])
    return 0.000001  # default to a small value

def get_min_notional(symbol_info):
    """Get MIN_NOTIONAL value"""
    for f in symbol_info['filters']:
        if f['filterType'] == 'MIN_NOTIONAL':
            return float(f['minNotional'])
    return 0.001

def round_step_size(quantity, step_size):
    """Round quantity to exchange step size"""
    if step_size == 0:
        return quantity
    return round(quantity // step_size * step_size, 8)

def round_price(price, tick_size):
    """Round price to exchange tick size"""
    if tick_size == 0:
        return price
    return round(price // tick_size * tick_size, 8)

def place_order(symbol, side, quantity, order_type, price=None):
    """Place an order and return order ID"""
    try:
        params = {
            'symbol': symbol,
            'side': side,
            'type': order_type,
            'quantity': quantity,
            'newOrderRespType': 'FULL'
        }
        
        if order_type == ORDER_TYPE_LIMIT:
            params['timeInForce'] = TIME_IN_FORCE_GTC
            params['price'] = price
        
        order = client.create_order(**params)
        return order['orderId']
    except Exception as e:
        print(f"Order placement failed: {e}")
        return None

def wait_for_order_completion(symbol, order_id, step_name):
    """Wait for order to be filled indefinitely"""
    global arbitrage_status
    
    # Add order to active orders
    arbitrage_status["active_orders"][order_id] = {
        "symbol": symbol,
        "step": step_name,
        "status": "PENDING"
    }
    
    while True:
        # Check if paused
        if arbitrage_status["paused"]:
            time.sleep(1)
            continue
            
        try:
            order_status = client.get_order(symbol=symbol, orderId=order_id)
            status = order_status['status']
            
            # Update order status
            arbitrage_status["active_orders"][order_id]["status"] = status
            
            if status == 'FILLED':
                print(f"Order {order_id} filled successfully")
                # Remove from active orders
                del arbitrage_status["active_orders"][order_id]
                return float(order_status['executedQty'])
            elif status in ['CANCELED', 'EXPIRED', 'REJECTED']:
                print(f"Order {order_id} was {status}")
                # Remove from active orders
                del arbitrage_status["active_orders"][order_id]
                return 0.0
                
            time.sleep(2)
        except Exception as e:
            print(f"Error checking order status: {e}")
            time.sleep(5)

def execute_arbitrage_task(pairs, usdt_amount, price1, price2, price3):
    """Execute triangular arbitrage strategy (to run in thread)"""
    global arbitrage_status
    
    # Clear previous logs
    arbitrage_status["logs"] = []
    arbitrage_status["running"] = True
    arbitrage_status["paused"] = False
    arbitrage_status["active_orders"] = {}
    
    def log_message(message):
        timestamp = time.strftime("%H:%M:%S")
        log_entry = f"{timestamp} - {message}"
        arbitrage_status["logs"].append(log_entry)
        print(log_entry)
    
    log_message("Starting arbitrage execution")
    
    # Get symbol information
    symbols = [get_symbol_info(p) for p in pairs]
    if any(s is None for s in symbols):
        log_message("Error: Invalid symbol information")
        arbitrage_status["running"] = False
        return

    # Extract asset names
    asset1 = symbols[0]['baseAsset']  # Asset to buy in first step
    quote1 = symbols[0]['quoteAsset']
    asset2 = symbols[1]['quoteAsset']  # Asset to get in second step
    quote2 = symbols[2]['quoteAsset']
    
    # Validate asset flow
    if quote1 != 'USDT' or quote2 != 'USDT':
        log_message("Error: First and last pair must be against USDT")
        arbitrage_status["running"] = False
        return
    if symbols[1]['baseAsset'] != asset1:
        log_message(f"Error: First asset must match base of second pair! Expected {asset1}, got {symbols[1]['baseAsset']}")
        arbitrage_status["running"] = False
        return
    if symbols[2]['baseAsset'] != asset2:
        log_message(f"Error: Second asset must match base of third pair! Expected {asset2}, got {symbols[2]['baseAsset']}")
        arbitrage_status["running"] = False
        return

    # Step 1: Buy first asset with USDT
    step1 = get_step_size(symbols[0])
    min_notional1 = get_min_notional(symbols[0])
    
    qty1 = round_step_size(usdt_amount / price1, step1)
    notional1 = qty1 * price1
    
    if notional1 < min_notional1:
        log_message(f"Error: Minimum notional not met for {pairs[0]}: {min_notional1}")
        arbitrage_status["running"] = False
        return

    log_message(f"Step 1: Buying {asset1} with USDT")
    log_message(f"Placing BUY order for {pairs[0]}: {qty1} @ {price1}")
    order_id1 = place_order(pairs[0], SIDE_BUY, qty1, ORDER_TYPE_LIMIT, price1)
    if not order_id1:
        log_message("Order placement failed for step 1")
        arbitrage_status["running"] = False
        return
    
    qty1_filled = wait_for_order_completion(pairs[0], order_id1, "Step 1: Buy Asset")
    if qty1_filled <= 0:
        log_message("First order failed")
        arbitrage_status["running"] = False
        return
    log_message(f"Order filled: Received {qty1_filled} {asset1}")

    # Step 2: Sell first asset to get second asset
    step2 = get_step_size(symbols[1])
    min_notional2 = get_min_notional(symbols[1])
    qty2 = round_step_size(qty1_filled, step2)
    notional2 = qty2 * price2
    
    if notional2 < min_notional2:
        log_message(f"Error: Minimum notional not met for {pairs[1]}: {min_notional2}")
        arbitrage_status["running"] = False
        return

    log_message(f"Step 2: Selling {asset1} to get {asset2}")
    log_message(f"Placing SELL order for {pairs[1]}: {qty2} @ {price2}")
    order_id2 = place_order(pairs[1], SIDE_SELL, qty2, ORDER_TYPE_LIMIT, price2)
    if not order_id2:
        log_message("Order placement failed for step 2")
        arbitrage_status["running"] = False
        return
    
    qty2_filled = wait_for_order_completion(pairs[1], order_id2, "Step 2: Trade Assets")
    if qty2_filled <= 0:
        log_message("Second order failed")
        arbitrage_status["running"] = False
        return
    
    # Calculate how much asset2 we received
    asset2_received = qty2_filled * price2
    log_message(f"Order filled: Received {asset2_received} {asset2}")

    # Step 3: Sell second asset to get USDT using LIMIT order
    step3 = get_step_size(symbols[2])
    min_notional3 = get_min_notional(symbols[2])
    qty3 = round_step_size(asset2_received, step3)
    notional3 = qty3 * price3
    
    if notional3 < min_notional3:
        log_message(f"Error: Minimum notional not met for {pairs[2]}: {min_notional3}")
        arbitrage_status["running"] = False
        return

    log_message(f"Step 3: Selling {asset2} to get USDT at limit price {price3}")
    log_message(f"Placing SELL order for {pairs[2]}: {qty3} @ {price3}")
    order_id3 = place_order(pairs[2], SIDE_SELL, qty3, ORDER_TYPE_LIMIT, price3)
    if not order_id3:
        log_message("Order placement failed for step 3")
        arbitrage_status["running"] = False
        return
    
    # Wait for order completion
    usdt_received = wait_for_order_completion(pairs[2], order_id3, "Step 3: Sell for USDT")
    log_message(f"Order filled: Received {usdt_received} USDT")
    
    log_message("Arbitrage execution completed!")
    log_message("Updating account balances...")
    update_balances()
    arbitrage_status["running"] = False
    arbitrage_status["current_step"] = "Execution completed"

def balance_update_thread():
    """Background thread to update balances"""
    global stop_balance_display
    while not stop_balance_display:
        update_balances()
        time.sleep(3)

@app.route('/')
def index():
    """Main application page"""
    return render_template('index.html')

@app.route('/get_balances', methods=['GET'])
def get_balances():
    """Return current balances"""
    return jsonify(balances)

@app.route('/execute_arbitrage', methods=['POST'])
def execute_arbitrage():
    """Execute arbitrage strategy"""
    if arbitrage_status["running"]:
        return jsonify({"status": "error", "message": "Arbitrage already in progress"})
    
    data = request.get_json()
    pairs = data.get('pairs', [])
    usdt_amount = float(data.get('usdt_amount', 0))
    price1 = float(data.get('price1', 0))
    price2 = float(data.get('price2', 0))
    price3 = float(data.get('price3', 0))
    
    if len(pairs) != 3:
        return jsonify({"status": "error", "message": "Exactly 3 trading pairs required"})
    
    # Pre-execution notional check
    symbols = [get_symbol_info(p) for p in pairs]
    if any(s is None for s in symbols):
        return jsonify({"status": "error", "message": "Invalid symbol information"})
    
    # Extract asset names for validation
    asset1 = symbols[0]['baseAsset']
    quote1 = symbols[0]['quoteAsset']
    asset2 = symbols[1]['quoteAsset']
    quote2 = symbols[2]['quoteAsset']
    
    # Validate asset flow
    if quote1 != 'USDT' or quote2 != 'USDT':
        return jsonify({"status": "error", "message": "First and last pair must be against USDT"})
    if symbols[1]['baseAsset'] != asset1:
        return jsonify({"status": "error", "message": f"First asset must match base of second pair! Expected {asset1}, got {symbols[1]['baseAsset']}"})
    if symbols[2]['baseAsset'] != asset2:
        return jsonify({"status": "error", "message": f"Second asset must match base of third pair! Expected {asset2}, got {symbols[2]['baseAsset']}"})
    
    # Step 1: Check if we can buy first asset with USDT
    step1 = get_step_size(symbols[0])
    min_notional1 = get_min_notional(symbols[0])
    qty1 = round_step_size(usdt_amount / price1, step1)
    notional1 = qty1 * price1
    
    if notional1 < min_notional1:
        return jsonify({"status": "error", "message": f"Minimum notional not met for {pairs[0]}: {min_notional1} (calculated: {notional1:.6f})"})
    
    # Step 2: Check if we can sell first asset to get second asset
    step2 = get_step_size(symbols[1])
    min_notional2 = get_min_notional(symbols[1])
    qty2 = round_step_size(qty1, step2)
    notional2 = qty2 * price2
    
    if notional2 < min_notional2:
        return jsonify({"status": "error", "message": f"Minimum notional not met for {pairs[1]}: {min_notional2} (calculated: {notional2:.6f})"})
    
    # Step 3: Check if we can sell second asset to get USDT
    # Calculate how much asset2 we would receive
    asset2_received = qty2 * price2
    
    step3 = get_step_size(symbols[2])
    min_notional3 = get_min_notional(symbols[2])
    qty3 = round_step_size(asset2_received, step3)
    notional3 = qty3 * price3
    
    if notional3 < min_notional3:
        return jsonify({"status": "error", "message": f"Minimum notional not met for {pairs[2]}: {min_notional3} (calculated: {notional3:.6f})"})
    
    # All notional checks passed, start arbitrage
    thread = threading.Thread(target=execute_arbitrage_task, args=(pairs, usdt_amount, price1, price2, price3))
    thread.daemon = True
    thread.start()
    
    return jsonify({"status": "success", "message": "Arbitrage started"})

@app.route('/arbitrage_status', methods=['GET'])
def get_arbitrage_status():
    """Return current arbitrage execution status"""
    return jsonify({
        "running": arbitrage_status["running"],
        "paused": arbitrage_status["paused"],
        "current_step": arbitrage_status["current_step"],
        "logs": arbitrage_status["logs"][-20:],  # Return last 20 logs
        "active_orders": arbitrage_status["active_orders"]
    })

@app.route('/pause_arbitrage', methods=['POST'])
def pause_arbitrage():
    """Pause or resume arbitrage execution"""
    data = request.get_json()
    pause = data.get('pause', True)
    arbitrage_status["paused"] = pause
    return jsonify({"status": "success", "paused": pause})

@app.route('/cancel_order', methods=['POST'])
def cancel_order():
    """Cancel a specific order"""
    if not arbitrage_status["running"]:
        return jsonify({"status": "error", "message": "No arbitrage in progress"})
    
    data = request.get_json()
    order_id = data.get('order_id')
    
    if order_id not in arbitrage_status["active_orders"]:
        return jsonify({"status": "error", "message": "Order not found"})
    
    symbol = arbitrage_status["active_orders"][order_id]["symbol"]
    
    try:
        client.cancel_order(symbol=symbol, orderId=order_id)
        # Update status
        arbitrage_status["active_orders"][order_id]["status"] = "CANCELED"
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

@app.route('/validate_opportunity', methods=['POST'])
def validate_opportunity():
    data = request.get_json()
    pairs = data.get('pairs', [])
    price1 = float(data.get('price1', 0))
    price2 = float(data.get('price2', 0))
    price3 = float(data.get('price3', 0))
    
    # Validate pairs
    for symbol in pairs:
        if not get_symbol_info(symbol):
            return jsonify({"valid": False, "message": f"Invalid symbol: {symbol}"})
    
    # Validate prices
    try:
        # Get current top of book for all pairs
        ticker1 = client.get_orderbook_ticker(symbol=pairs[0])
        ticker2 = client.get_orderbook_ticker(symbol=pairs[1])
        ticker3 = client.get_orderbook_ticker(symbol=pairs[2])
        
        current_ask1 = float(ticker1['askPrice'])
        current_ask2 = float(ticker2['askPrice'])
        current_bid3 = float(ticker3['bidPrice'])
        
        # Allow some slippage (1%)
        slippage = 0.01
        if abs(price1 - current_ask1) / current_ask1 > slippage:
            return jsonify({"valid": False, "message": f"Price for {pairs[0]} changed: expected {price1}, got {current_ask1}"})
            
        if abs(price2 - current_ask2) / current_ask2 > slippage:
            return jsonify({"valid": False, "message": f"Price for {pairs[1]} changed: expected {price2}, got {current_ask2}"})
            
        if abs(price3 - current_bid3) / current_bid3 > slippage:
            return jsonify({"valid": False, "message": f"Price for {pairs[2]} changed: expected {price3}, got {current_bid3}"})
            
        return jsonify({"valid": True})
        
    except Exception as e:
        return jsonify({"valid": False, "message": str(e)})

@app.route('/check_price_stability', methods=['POST'])
def check_price_stability():
    """Check if prices are still stable since opportunity was found"""
    data = request.get_json()
    pairs = data.get('pairs', [])
    expected_prices = data.get('prices', [])
    
    try:
        # Get current prices
        current_prices = []
        for pair in pairs:
            ticker = client.get_orderbook_ticker(symbol=pair)
            # For buy orders, use ask price; for sell orders, use bid price
            if pair == pairs[0] or pair == pairs[1]:  # First two are buy orders
                current_prices.append(float(ticker['askPrice']))
            else:  # Last is sell order
                current_prices.append(float(ticker['bidPrice']))
        
        # Check if prices are within 0.5% of expected
        stable = True
        for i, (current, expected) in enumerate(zip(current_prices, expected_prices)):
            if abs(current - expected) / expected > 0.005:  # 0.5% threshold
                stable = False
                break
                
        return jsonify({"stable": stable, "current_prices": current_prices})
        
    except Exception as e:
        return jsonify({"stable": False, "error": str(e)})

@app.route('/get_trading_volume', methods=['POST'])
def get_trading_volume():
    """Get recent trading volume for pairs"""
    data = request.get_json()
    pairs = data.get('pairs', [])
    
    try:
        volumes = {}
        for pair in pairs:
            # Get 24h ticker for volume
            ticker = client.get_ticker(symbol=pair)
            volumes[pair] = float(ticker['volume'])
            
        return jsonify(volumes)
    except Exception as e:
        return jsonify({"error": str(e)})

@app.route('/get_orderbook_depth', methods=['POST'])
def get_orderbook_depth():
    """Get orderbook depth for pairs"""
    data = request.get_json()
    pairs = data.get('pairs', [])
    limit = data.get('limit', 5)  # Default to top 5 levels
    
    try:
        orderbooks = {}
        for pair in pairs:
            depth = client.get_order_book(symbol=pair, limit=limit)
            orderbooks[pair] = {
                'asks': depth['asks'],
                'bids': depth['bids']
            }
            
        return jsonify(orderbooks)
    except Exception as e:
        return jsonify({"error": str(e)})

if __name__ == "__main__":
    # Start balance update thread
    balance_thread = threading.Thread(target=balance_update_thread, daemon=True)
    balance_thread.start()
    
    # Initial balance update
    update_balances()
    
    app.run(debug=os.environ.get('FLASK_DEBUG', 'False') == 'True', use_reloader=False)