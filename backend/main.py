import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List
import httpx
import sqlite3
import os
import secrets
import traceback
import hashlib
from dotenv import load_dotenv
import time
import asyncio

from bitcoinutils.setup import setup
from bitcoinutils.keys import PrivateKey, PublicKey, P2wshAddress, P2wpkhAddress, P2trAddress
from bitcoinutils.transactions import Transaction, TxInput, TxOutput, TxWitnessInput
from bitcoinutils.script import Script
from bitcoinutils.constants import NETWORK_SEGWIT_PREFIXES
from bitcoinutils.utils import tapleaf_tagged_hash, tweak_taproot_pubkey, ControlBlock, get_tag_hashed_merkle_root

from contextlib import asynccontextmanager

# --- 1. 設定比特幣環境 ---
setup('testnet') 
load_dotenv() 

async def auto_settle_all(difficulty):
    contracts = db_get_pending_contracts()
    if not contracts: return
    print(f"Auto-settling {len(contracts)} contracts with difficulty {difficulty}...")
    for contract in contracts:
        await execute_settlement(contract, difficulty)

async def background_monitor():
    last_block_hash = ""
    print("Background monitor started.")
    while True:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get("https://mempool.space/signet/api/blocks/tip/hash")
                if resp.status_code == 200:
                    current_hash = resp.text
                    if last_block_hash and current_hash != last_block_hash:
                        print(f"New block detected: {current_hash}")
                        # Calculate difficulty
                        seed = int(current_hash[-4:], 16)
                        normalized = seed / 65535.0 
                        difficulty = 0.01 + (normalized * 0.08)
                        difficulty = round(difficulty, 4)
                        
                        print(f"New Difficulty: {difficulty}. Triggering Auto-Settlement...")
                        await auto_settle_all(difficulty)
                        
                    last_block_hash = current_hash
        except Exception as e:
            print(f"Monitor Error: {e}")
            
        await asyncio.sleep(60)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    init_db()
    asyncio.create_task(background_monitor())
    yield
    # Shutdown (if needed)

app = FastAPI(title="HashHedge Trust-Minimized Oracle", lifespan=lifespan)

# 允許 CORS，方便 Vue 前端呼叫
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- WebSocket Manager ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except:
                pass

manager = ConnectionManager()

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)

DB_NAME = "hashhedge_oracle.db"

# --- 模擬 House 和 Oracle 的私鑰 ---
# 從環境變數讀取，若無則使用預設值 (僅供測試)
HOUSE_SECRET = int(os.getenv("HOUSE_KEY_SECRET"))
ORACLE_SECRET = int(os.getenv("ORACLE_KEY_SECRET"))

HOUSE_PRIV_KEY = PrivateKey(secret_exponent=HOUSE_SECRET) 
HOUSE_PUB_KEY_HEX = HOUSE_PRIV_KEY.get_public_key().to_hex()

ORACLE_PRIV_KEY = PrivateKey(secret_exponent=ORACLE_SECRET)
ORACLE_PUB_KEY_HEX = ORACLE_PRIV_KEY.get_public_key().to_hex()

# BIP341 NUMS point (lift_x(0x50929b...))
# We use the bytes directly for internal key
NUMS_PUBKEY_HEX = "50929b74c1a04954b78b4b6035e97a5e078a5a0f28ec96d547bfee9ace803ac0"

def to_x_only(pubkey_hex):
    if len(pubkey_hex) == 130 and pubkey_hex.startswith('04'):
        return pubkey_hex[2:66]
    elif len(pubkey_hex) == 66:
        return pubkey_hex[2:]
    return pubkey_hex

# --- 2. 資料庫 ---
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    # 我們現在存的是 redeem_script (解鎖腳本)，而不是私鑰
    c.execute('''
        CREATE TABLE IF NOT EXISTS contracts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_pubkey TEXT NOT NULL,
            deposit_address TEXT NOT NULL,
            redeem_script_hex TEXT NOT NULL,
            amount INTEGER NOT NULL,
            direction TEXT NOT NULL,
            status TEXT DEFAULT 'PENDING',
            tx_hex TEXT,
            nonce TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            block_height INTEGER
        )
    ''')
    # 嘗試為舊資料庫新增欄位 (如果不存在)
    try:
        c.execute("ALTER TABLE contracts ADD COLUMN nonce TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE contracts ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")
    except sqlite3.OperationalError:
        pass
    try:
        c.execute("ALTER TABLE contracts ADD COLUMN block_height INTEGER")
    except sqlite3.OperationalError:
        pass

    conn.commit()
    conn.close()

def db_create_contract(user_pub, address, script_hex, amount, direction, nonce, block_height):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''
        INSERT INTO contracts (user_pubkey, deposit_address, redeem_script_hex, amount, direction, nonce, block_height)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (user_pub, address, script_hex, amount, direction, nonce, block_height))
    order_id = c.lastrowid
    conn.commit()
    conn.close()
    return order_id

def db_get_pending_contracts():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM contracts WHERE status IN ('PENDING', 'WAITING_USER_SIG')")
    rows = c.fetchall()
    conn.close()
    return [dict(row) for row in rows]

def db_get_contract(order_id):
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM contracts WHERE id = ?", (order_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None

def db_get_contracts_by_user(user_pubkey):
    """ 根據用戶公鑰查詢所有合約 """
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM contracts WHERE user_pubkey = ? ORDER BY id DESC", (user_pubkey,))
    rows = c.fetchall()
    conn.close()
    return [dict(row) for row in rows]

def db_update_status(order_id, status, tx_hex=None):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    if tx_hex:
        c.execute("UPDATE contracts SET status = ?, tx_hex = ? WHERE id = ?", (status, tx_hex, order_id))
    else:
        c.execute("UPDATE contracts SET status = ? WHERE id = ?", (status, order_id))
    conn.commit()
    conn.close()

def db_delete_contract(order_id):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("DELETE FROM contracts WHERE id = ?", (order_id,))
    conn.commit()
    conn.close()

# --- 3. 比特幣核心邏輯 (Multisig) ---

def create_contract_tree(user_pub, house_pub, oracle_pub, nonce_hex):
    """
    建立 MAST 樹狀結構 (Win, Loss, Refund)
    Win: User + Oracle
    Loss: House + Oracle
    Refund: User + House
    Structure: [[Win, Loss], Refund]
    """
    user_x = to_x_only(user_pub)
    house_x = to_x_only(house_pub)
    oracle_x = to_x_only(oracle_pub)

    def make_2of2_script(pk1, pk2, nonce):
        # <nonce> OP_DROP <pk1> OP_CHECKSIG <pk2> OP_CHECKSIGADD OP_2 OP_NUMEQUAL
        pks = sorted([pk1, pk2])
        return Script([
            nonce, 'OP_DROP',
            pks[0], 'OP_CHECKSIG',
            pks[1], 'OP_CHECKSIGADD',
            'OP_2', 'OP_NUMEQUAL'
        ])

    script_win = make_2of2_script(user_x, oracle_x, nonce_hex)
    script_loss = make_2of2_script(house_x, oracle_x, nonce_hex)
    script_refund = make_2of2_script(user_x, house_x, nonce_hex)

    # 構建 MAST Tree: [[Win, Loss], Refund]
    tree = [[script_win, script_loss], script_refund]
    return tree, script_win, script_loss, script_refund

def create_2of3_address(user_pubkey_hex, nonce_hex):
    """ 建立基於 MAST 的 Taproot 地址 """
    tree, _, _, _ = create_contract_tree(user_pubkey_hex, HOUSE_PUB_KEY_HEX, ORACLE_PUB_KEY_HEX, nonce_hex)
    
    # Use PublicKey to get full 64-byte representation (X+Y) for tweaking
    internal_pub = PublicKey(NUMS_PUBKEY_HEX)
    internal_pub_bytes = internal_pub.to_bytes()
    
    # Calculate Root Hash
    root_hash = get_tag_hashed_merkle_root(tree)
    tweak = int.from_bytes(root_hash, 'big')
    
    tweaked_pubkey, _ = tweak_taproot_pubkey(internal_pub_bytes, tweak)
    
    # Generate P2TR Address
    addr = P2trAddress(witness_program=tweaked_pubkey[:32].hex())
    return addr.to_string(), ""

async def get_utxos(address):
    base_url = "https://mempool.space/signet/api"
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(f"{base_url}/address/{address}/utxo")
            if resp.status_code != 200: return []
            return resp.json()
        except:
            return []

async def build_win_path_partial_tx(contract, to_address):
    """ 構建 User Win 的部分簽名交易 (Oracle 簽名, User 留空) """
    # 1. Reconstruct Tree
    tree, script_win, _, _ = create_contract_tree(
        contract['user_pubkey'], HOUSE_PUB_KEY_HEX, ORACLE_PUB_KEY_HEX, contract['nonce']
    )
    
    # 2. Get Control Block for WIN branch (Index 0 in [[Win, Loss], Refund])
    internal_pub = PublicKey(NUMS_PUBKEY_HEX)
    internal_pub_bytes = internal_pub.to_bytes()
    
    root_hash = get_tag_hashed_merkle_root(tree)
    tweak = int.from_bytes(root_hash, 'big')
    _, parity = tweak_taproot_pubkey(internal_pub_bytes, tweak)
    
    cb = ControlBlock(internal_pub, tree, 0, is_odd=(parity == 1))
    
    # 3. Get UTXOs
    utxos = await get_utxos(contract['deposit_address'])
    if not utxos:
        raise ValueError("Contract address has no funds")

    tx_inputs = []
    total_in = 0
    
    tweaked_pubkey, _ = tweak_taproot_pubkey(internal_pub_bytes, tweak)
    tr_addr = P2trAddress(witness_program=tweaked_pubkey[:32].hex())
    utxo_script_pubkey = tr_addr.to_script_pub_key()
    
    for utxo in utxos:
        tx_inputs.append(TxInput(utxo['txid'], utxo['vout']))
        total_in += utxo['value']

    # 4. Fee Calculation
    est_vbytes = (len(tx_inputs) * 150) + (1 * 31) + 11 
    fee_rate = 2.0
    fee = int(est_vbytes * fee_rate)
    
    send_amount = total_in - fee
    if send_amount <= 0: raise ValueError(f"Insufficient funds for fee")

    # 5. Output
    dest_script = to_address.to_script_pub_key()
    tx_output = TxOutput(send_amount, dest_script)
    tx = Transaction(tx_inputs, [tx_output], has_segwit=True)

    # 6. Sign (Oracle Only)
    user_x = to_x_only(contract['user_pubkey'])
    oracle_x = to_x_only(ORACLE_PUB_KEY_HEX)
    
    # Sort keys to match script order
    pubkeys = sorted([user_x, oracle_x])
    
    for i, utxo in enumerate(utxos):
        amount = utxo['value']
        
        # Sign with Oracle Key
        sig_oracle = ORACLE_PRIV_KEY.sign_taproot_input(
            tx, i, [utxo_script_pubkey], [amount],
            script_path=True, tapleaf_script=script_win, tweak=False
        )
        
        sigs_map = {
            oracle_x: sig_oracle
        }
        
        # Witness Stack: [Sig2, Sig1] (Reverse order)
        witness_stack = []
        for pk in reversed(pubkeys):
            if pk in sigs_map:
                witness_stack.append(sigs_map[pk])
            else:
                witness_stack.append("") # Placeholder for User signature
        
        # Witness: [Stack Elements..., Script, Control Block]
        witness_elements = witness_stack + [script_win.to_hex(), cb.to_hex()]
        tx.witnesses.append(TxWitnessInput(witness_elements))

    return tx.serialize()

async def build_multisig_spend(contract, to_address):
    """ 構建 Taproot Script Path 花費交易 (House + Oracle 簽名 -> LOSS Branch) """
    # 1. Reconstruct Tree and Scripts
    tree, _, script_loss, _ = create_contract_tree(
        contract['user_pubkey'], HOUSE_PUB_KEY_HEX, ORACLE_PUB_KEY_HEX, contract['nonce']
    )
    
    # 2. Get Control Block for LOSS branch (Index 1 in [[Win, Loss], Refund])
    internal_pub = PublicKey(NUMS_PUBKEY_HEX)
    internal_pub_bytes = internal_pub.to_bytes()
    
    root_hash = get_tag_hashed_merkle_root(tree)
    tweak = int.from_bytes(root_hash, 'big')
    _, parity = tweak_taproot_pubkey(internal_pub_bytes, tweak)
    
    cb = ControlBlock(internal_pub, tree, 1, is_odd=(parity == 1))
    
    # 3. Get UTXOs
    utxos = await get_utxos(contract['deposit_address'])
    if not utxos:
        raise ValueError("Contract address has no funds (尚未入金?)")

    tx_inputs = []
    total_in = 0
    
    # Output Pubkey (Tweaked)
    tweaked_pubkey, _ = tweak_taproot_pubkey(internal_pub_bytes, tweak)
    tr_addr = P2trAddress(witness_program=tweaked_pubkey[:32].hex())
    utxo_script_pubkey = tr_addr.to_script_pub_key()
    
    for utxo in utxos:
        tx_inputs.append(TxInput(utxo['txid'], utxo['vout']))
        total_in += utxo['value']

    # 4. Fee Calculation
    est_vbytes = (len(tx_inputs) * 150) + (1 * 31) + 11 
    fee_rate = 2.0
    fee = int(est_vbytes * fee_rate)
    
    print(f"Estimated vBytes: {est_vbytes}, Fee: {fee} sats")

    send_amount = total_in - fee
    if send_amount <= 0: raise ValueError(f"Insufficient funds for fee (Need {fee}, Has {total_in})")

    # 5. Output
    dest_script = to_address.to_script_pub_key()
    tx_output = TxOutput(send_amount, dest_script)
    tx = Transaction(tx_inputs, [tx_output], has_segwit=True)

    # 6. Sign (House + Oracle) for LOSS branch
    house_x = to_x_only(HOUSE_PUB_KEY_HEX)
    oracle_x = to_x_only(ORACLE_PUB_KEY_HEX)
    
    # Sort keys to match script order
    pubkeys = sorted([house_x, oracle_x])
    
    for i, utxo in enumerate(utxos):
        amount = utxo['value']
        
        # Sign with House Key
        sig_house = HOUSE_PRIV_KEY.sign_taproot_input(
            tx, i, [utxo_script_pubkey], [amount],
            script_path=True, tapleaf_script=script_loss, tweak=False
        )
        
        # Sign with Oracle Key
        sig_oracle = ORACLE_PRIV_KEY.sign_taproot_input(
            tx, i, [utxo_script_pubkey], [amount],
            script_path=True, tapleaf_script=script_loss, tweak=False
        )
        
        sigs_map = {
            house_x: sig_house,
            oracle_x: sig_oracle
        }
        
        # Witness Stack: [Sig2, Sig1] (Reverse order of pubkeys in script)
        witness_stack = []
        for pk in reversed(pubkeys):
            if pk in sigs_map:
                witness_stack.append(sigs_map[pk])
            else:
                # Empty signature must be empty bytes (represented as empty hex string)
                witness_stack.append("") 
        
        # Witness: [Stack Elements..., Script, Control Block]
        witness_elements = witness_stack + [script_loss.to_hex(), cb.to_hex()]
        tx.witnesses.append(TxWitnessInput(witness_elements))

    return tx.serialize()

async def broadcast_tx(tx_hex):
    url = "https://mempool.space/signet/api/tx"
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(url, data=tx_hex)
            return resp.text 
        except Exception as e:
            return str(e)

# --- 新增功能: House 對賭與退款邏輯 ---

async def send_funds_from_house(to_address_obj, amount_sats):
    """ 從 House 發送資金 """
    # 1. 獲取 House UTXO
    house_pub = HOUSE_PRIV_KEY.get_public_key()
    house_addr = house_pub.get_segwit_address()
    utxos = await get_utxos(house_addr.to_string())
    
    if not utxos:
        raise ValueError("House wallet has no funds! Please fund the House address first.")

    # 2. 準備 Inputs
    tx_inputs = []
    total_in = 0
    for utxo in utxos:
        tx_inputs.append(TxInput(utxo['txid'], utxo['vout']))
        total_in += utxo['value']

    # 3. 準備 Outputs
    # P2WPKH Input ~68 vbytes, P2WSH Output ~43 vbytes, Change ~31 vbytes, Overhead ~11
    est_vbytes = (len(tx_inputs) * 68) + 43 + 31 + 11
    fee = int(est_vbytes * 2.0)
    change = total_in - amount_sats - fee
    
    if change < 0:
         raise ValueError(f"House insufficient funds. Has {total_in}, needs {amount_sats+fee}")

    outputs = []
    # Output 1: 轉給目標 (多簽合約)
    outputs.append(TxOutput(amount_sats, to_address_obj.to_script_pub_key()))
    
    # Output 2: 找零回 House
    if change > 546: # Dust limit
        outputs.append(TxOutput(change, house_addr.to_script_pub_key()))

    tx = Transaction(tx_inputs, outputs, has_segwit=True)

    # 4. 簽名 (P2WPKH)
    # P2WPKH 簽名時，script_code 必須是該公鑰對應的 P2PKH Script
    p2pkh_script = house_pub.get_address().to_script_pub_key()

    for i, utxo in enumerate(utxos):
        sig = HOUSE_PRIV_KEY.sign_segwit_input(tx, i, p2pkh_script, utxo['value'])
        tx.witnesses.append(TxWitnessInput([sig, house_pub.to_hex()]))
        
    return tx.serialize()

async def build_refund_tx(contract):
    """ 構建 Taproot 退款交易 (User + House 簽名 -> Refund Branch) """
    # 1. Reconstruct Tree
    tree, _, _, script_refund = create_contract_tree(
        contract['user_pubkey'], HOUSE_PUB_KEY_HEX, ORACLE_PUB_KEY_HEX, contract['nonce']
    )
    
    # 2. Get Control Block for REFUND branch (Index 2)
    internal_pub = PublicKey(NUMS_PUBKEY_HEX)
    internal_pub_bytes = internal_pub.to_bytes()
    root_hash = get_tag_hashed_merkle_root(tree)
    tweak = int.from_bytes(root_hash, 'big')
    _, parity = tweak_taproot_pubkey(internal_pub_bytes, tweak)
    
    cb = ControlBlock(internal_pub, tree, 2, is_odd=(parity == 1))
    
    # 3. Get UTXOs
    utxos = await get_utxos(contract['deposit_address'])
    if not utxos:
        raise ValueError("Contract address has no funds")

    tx_inputs = []
    total_in = 0
    
    tweaked_pubkey, _ = tweak_taproot_pubkey(internal_pub_bytes, tweak)
    tr_addr = P2trAddress(witness_program=tweaked_pubkey[:32].hex())
    utxo_script_pubkey = tr_addr.to_script_pub_key()
    
    for utxo in utxos:
        tx_inputs.append(TxInput(utxo['txid'], utxo['vout']))
        total_in += utxo['value']

    # 4. Refund Logic
    amount = contract['amount']
    est_vbytes = (len(tx_inputs) * 150) + (2 * 31) + 11
    fee = int(est_vbytes * 2.0)
    
    outputs = []
    msg = ""
    
    if total_in >= amount * 2:
        # Split Refund
        refund_amount = (total_in - fee) // 2
        user_addr = PublicKey(contract['user_pubkey']).get_segwit_address()
        house_addr = HOUSE_PRIV_KEY.get_public_key().get_segwit_address()
        
        outputs.append(TxOutput(refund_amount, user_addr.to_script_pub_key()))
        outputs.append(TxOutput(refund_amount, house_addr.to_script_pub_key()))
        msg = "Refunded 50/50 to User and House (Partial TX)"
    else:
        # Full Refund to User
        refund_amount = total_in - fee
        user_addr = PublicKey(contract['user_pubkey']).get_segwit_address()
        outputs.append(TxOutput(refund_amount, user_addr.to_script_pub_key()))
        msg = "Refunded all to User (Partial TX)"

    tx = Transaction(tx_inputs, outputs, has_segwit=True)

    # 5. Sign (House Only) - User must sign later
    user_x = to_x_only(contract['user_pubkey'])
    house_x = to_x_only(HOUSE_PUB_KEY_HEX)
    
    pubkeys = sorted([user_x, house_x])
    
    for i, utxo in enumerate(utxos):
        amount_sats = utxo['value']
        
        sig_house = HOUSE_PRIV_KEY.sign_taproot_input(
            tx, i, [utxo_script_pubkey], [amount_sats],
            script_path=True, tapleaf_script=script_refund, tweak=False
        )
        
        sigs_map = {
            house_x: sig_house
        }
        
        witness_stack = []
        for pk in reversed(pubkeys):
            if pk in sigs_map:
                witness_stack.append(sigs_map[pk])
            else:
                witness_stack.append("") # User signature missing
        
        witness_elements = witness_stack + [script_refund.to_hex(), cb.to_hex()]
        tx.witnesses.append(TxWitnessInput(witness_elements))

    return tx.serialize(), msg

async def build_batch_win_tx(user_pubkey):
    """ 構建批量領取交易 (Batch Claim) """
    # 1. 找出該用戶所有等待簽名的合約
    contracts = db_get_contracts_by_user(user_pubkey)
    waiting_contracts = [c for c in contracts if c['status'] == 'WAITING_USER_SIG']
    
    if not waiting_contracts:
        return None, "No waiting contracts found"

    # 2. 準備 Inputs
    tx_inputs = []
    total_in = 0
    input_details = [] # 用來暫存每個 Input 對應的簽名資訊
    
    # 假設所有合約都匯款到同一個 User Address (由 Pubkey 衍生)
    user_addr_obj = PublicKey(user_pubkey).get_segwit_address()
    
    for contract in waiting_contracts:
        utxos = await get_utxos(contract['deposit_address'])
        if not utxos: continue 
        
        # 重建該合約的 Tree 與 Script
        tree, script_win, _, _ = create_contract_tree(
            contract['user_pubkey'], HOUSE_PUB_KEY_HEX, ORACLE_PUB_KEY_HEX, contract['nonce']
        )
        
        # 計算該合約的 Tweaked Pubkey (用於 Script Pubkey)
        internal_pub = PublicKey(NUMS_PUBKEY_HEX)
        internal_pub_bytes = internal_pub.to_bytes()
        root_hash = get_tag_hashed_merkle_root(tree)
        tweak = int.from_bytes(root_hash, 'big')
        tweaked_pubkey, parity = tweak_taproot_pubkey(internal_pub_bytes, tweak)
        
        tr_addr = P2trAddress(witness_program=tweaked_pubkey[:32].hex())
        utxo_script_pubkey = tr_addr.to_script_pub_key()
        
        # 取得 Win 分支的 Control Block
        cb = ControlBlock(internal_pub, tree, 0, is_odd=(parity == 1))
        
        for utxo in utxos:
            tx_inputs.append(TxInput(utxo['txid'], utxo['vout']))
            total_in += utxo['value']
            # 儲存簽名所需的資訊
            input_details.append({
                'amount': utxo['value'],
                'script_pubkey': utxo_script_pubkey,
                'script_win': script_win,
                'control_block': cb
            })

    if not tx_inputs:
        return None, "No funds found in waiting contracts"

    # 3. 計算手續費與輸出
    # 估算: 每個 Input 約 150 vbytes (Taproot Script Path) + Overhead
    est_vbytes = (len(tx_inputs) * 150) + 31 + 11
    fee = int(est_vbytes * 2.0)
    send_amount = total_in - fee
    
    if send_amount <= 0:
        return None, "Insufficient funds for fee"
        
    tx_output = TxOutput(send_amount, user_addr_obj.to_script_pub_key())
    tx = Transaction(tx_inputs, [tx_output], has_segwit=True)
    
    # 4. 對每個 Input 進行 Oracle 簽名
    user_x = to_x_only(user_pubkey)
    oracle_x = to_x_only(ORACLE_PUB_KEY_HEX)
    pubkeys = sorted([user_x, oracle_x])
    
    for i, detail in enumerate(input_details):
        # Oracle 簽名
        sig_oracle = ORACLE_PRIV_KEY.sign_taproot_input(
            tx, i, [detail['script_pubkey']], [detail['amount']],
            script_path=True, tapleaf_script=detail['script_win'], tweak=False
        )
        
        sigs_map = { oracle_x: sig_oracle }
        
        witness_stack = []
        for pk in reversed(pubkeys):
            if pk in sigs_map:
                witness_stack.append(sigs_map[pk])
            else:
                witness_stack.append("") # User 簽名留空
                
        witness_elements = witness_stack + [detail['script_win'].to_hex(), detail['control_block'].to_hex()]
        tx.witnesses.append(TxWitnessInput(witness_elements))
        
    return tx.serialize(), f"Batch transaction for {len(waiting_contracts)} contracts created."

# --- 4. API 介面 ---

class ContractRequest(BaseModel):
    user_pubkey: str  # 前端必須傳來 User 的公鑰 (Hex)
    amount: int
    direction: str

class ClaimAllRequest(BaseModel):
    user_pubkey: str

class SettleRequest(BaseModel):
    contract_id: int
    current_difficulty: float

class MatchRequest(BaseModel):
    contract_id: int

class RefundRequest(BaseModel):
    contract_id: int

class CancelRequest(BaseModel):
    contract_id: int

@app.get("/api/stats")
async def stats():
    # 顯示 House 地址，方便您打入測試幣
    house_addr = HOUSE_PRIV_KEY.get_public_key().get_segwit_address().to_string()
    
    # Dynamic Difficulty based on Signet Block Hash
    difficulty = 0.047 # Default
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get("https://mempool.space/signet/api/blocks/tip/hash")
            if resp.status_code == 200:
                block_hash = resp.text
                # Use last 4 hex chars to generate a float 0.0-1.0
                seed = int(block_hash[-4:], 16)
                # Normalize to range 0.01 - 0.09 (Target is usually around 0.05)
                normalized = seed / 65535.0 
                difficulty = 0.01 + (normalized * 0.08)
                difficulty = round(difficulty, 4)
    except:
        pass

    return {
        "difficulty": difficulty, 
        "hashprice_sats": 220000.0,
        "house_address": house_addr
    }

@app.get("/api/contract/{contract_id}")
def get_contract_api(contract_id: int):
    contract = db_get_contract(contract_id)
    if not contract:
        raise HTTPException(status_code=404, detail="Contract not found")
    return contract

@app.get("/api/contracts/user/{user_pubkey}")
def get_user_contracts(user_pubkey: str):
    """ 獲取特定用戶的所有合約 """
    contracts = db_get_contracts_by_user(user_pubkey)
    return {
        "count": len(contracts),
        "contracts": contracts
    }

@app.post("/api/claim_all")
async def claim_all_wins(req: ClaimAllRequest):
    """ 批量領取所有獲勝合約 """
    try:
        tx_hex, msg = await build_batch_win_tx(req.user_pubkey)
        if not tx_hex:
             return {"status": "error", "message": msg}
             
        return {
            "status": "ready_to_sign",
            "tx_hex": tx_hex,
            "message": msg
        }
    except Exception as e:
        print(traceback.format_exc())
        return {"status": "error", "error": str(e)}

@app.post("/api/create_contract")
async def create_contract(req: ContractRequest):
    # 生成隨機 Nonce (4 bytes)
    nonce_hex = secrets.token_hex(4)

    # 1. 根據 User 公鑰生成 2-of-3 地址
    address, script_hex = create_2of3_address(req.user_pubkey, nonce_hex)
    
    # 2. 獲取當前區塊高度
    try:
        status_info = await get_time_since_last_block()
        block_height = status_info['block_height']
    except:
        block_height = 0 # Fallback if API fails

    # 3. 存入 DB
    contract_id = db_create_contract(req.user_pubkey, address, script_hex, req.amount, req.direction, nonce_hex, block_height)
    
    return {
        "status": "success",
        "contract_id": contract_id,
        "deposit_address": address,
        "amount": req.amount,
        "message": f"Please deposit {req.amount} sats to this address. House will match 1:1."
    }

@app.post("/api/match")
async def match_contract(req: MatchRequest):
    try:
        contract = db_get_contract(req.contract_id)
        if not contract: raise HTTPException(404, "Contract not found")
        
        # 1. 檢查合約地址餘額 (確認 User 是否已入金)
        utxos = await get_utxos(contract['deposit_address'])
        current_balance = sum(u['value'] for u in utxos)
        
        # 簡單邏輯：如果餘額小於合約金額，代表 User 還沒入金
        if current_balance < contract['amount']:
            return {"status": "waiting_for_user", "message": "User deposit not detected yet."}
            
        # 如果餘額已經大於等於 2倍金額，代表 House 已經入金過了
        if current_balance >= contract['amount'] * 2:
            return {"status": "already_matched", "message": "Contract is already fully funded."}

        # 2. 重建多簽地址物件 (Sorted)
        user_x = to_x_only(contract['user_pubkey'])
        house_x = to_x_only(HOUSE_PUB_KEY_HEX)
        oracle_x = to_x_only(ORACLE_PUB_KEY_HEX)
        
        pubkeys = sorted([user_x, house_x, oracle_x])
        nonce_hex = contract['nonce']
        
        script_elements = [
            nonce_hex, 'OP_DROP',
            pubkeys[0], 'OP_CHECKSIG',
            pubkeys[1], 'OP_CHECKSIGADD',
            pubkeys[2], 'OP_CHECKSIGADD',
            'OP_2', 'OP_NUMEQUAL'
        ]
        tapleaf_script = Script(script_elements)
        
        leaf_hash = tapleaf_tagged_hash(tapleaf_script)
        internal_pub = PublicKey(NUMS_PUBKEY_HEX)
        internal_pub_bytes = internal_pub.to_bytes()
        tweak = int.from_bytes(leaf_hash, 'big')
        tweaked_pubkey, _ = tweak_taproot_pubkey(internal_pub_bytes, tweak)
        
        multisig_addr = P2trAddress(witness_program=tweaked_pubkey[:32].hex())
        
        # 3. House 發送對賭資金 (1:1 賠率，金額與 User 相同)
        match_amount = contract['amount'] 
        
        tx_hex = await send_funds_from_house(multisig_addr, match_amount)
        txid = await broadcast_tx(tx_hex)
        
        if len(txid) != 64:
             return {"status": "error", "error": "Broadcast failed", "details": txid}

        # Broadcast Match Event
        await manager.broadcast({
            "type": "MATCHED",
            "contract_id": req.contract_id,
            "txid": txid,
            "message": f"House matched {match_amount} sats."
        })

        return {
            "status": "matched", 
            "txid": txid, 
            "message": f"House matched {match_amount} sats (1:1 Odds). Contract is now live!"
        }
    except Exception as e:
        print(traceback.format_exc())
        return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

@app.post("/api/refund")
async def refund_contract(req: RefundRequest):
    try:
        contract = db_get_contract(req.contract_id)
        if not contract: raise HTTPException(404, "Contract not found")
        
        if contract['status'] != 'PENDING':
             return {"result": "ALREADY_SETTLED", "message": f"Contract is {contract['status']}"}

        tx_hex, msg = await build_refund_tx(contract)
        
        # Refund is now a partial TX (House signed, User needs to sign)
        status = "WAITING_USER_SIG_REFUND"
        db_update_status(req.contract_id, status, tx_hex)
        
        return {
            "status": "waiting_user_sig",
            "tx_hex": tx_hex,
            "message": msg + ". Waiting for User signature."
        }
    except Exception as e:
        print(traceback.format_exc())
        return {"status": "error", "error": str(e), "traceback": traceback.format_exc()}

@app.post("/api/cancel_contract")
def cancel_contract(req: CancelRequest):
    db_delete_contract(req.contract_id)
    return {"status": "cancelled", "contract_id": req.contract_id}

class SettleAllRequest(BaseModel):
    current_difficulty: float

async def execute_settlement(contract, current_difficulty):
    """ 執行單一合約結算邏輯 """
    
    # --- FIX START: 防止狀態翻轉 (Decision Locking) ---
    # 如果狀態已經是 WAITING_USER_SIG，代表 Oracle 之前已經判定 User 贏了。
    # 此時不應該再根據新的 difficulty 重新判斷，而是直接回傳之前的結果，讓用戶繼續簽名。
    if contract['status'] == 'WAITING_USER_SIG':
        # 確保有 tx_hex
        tx_hex = contract['tx_hex']
        if not tx_hex:
            # 如果資料庫沒存到，重新生成一次 Win TX (基於合約參數，結果是一樣的)
            user_addr_obj = PublicKey(contract['user_pubkey']).get_segwit_address()
            tx_hex = await build_win_path_partial_tx(contract, user_addr_obj)
            # 更新回 DB
            db_update_status(contract['id'], 'WAITING_USER_SIG', tx_hex)

        msg = "Oracle already signed (Locked). Waiting for User signature."
        return {
            "result": "WAITING_USER_SIG", 
            "tx_hex": tx_hex,
            "message": msg
        }
    # --- FIX END ---

    # 防止重複結算 (允許 WAITING_USER_SIG 狀態重試，以便更新手續費或重新獲取)
    if contract['status'] not in ['PENDING', 'WAITING_USER_SIG']: 
        return {"result": "ALREADY_SETTLED", "message": f"Contract is {contract['status']}"}

    # 判定輸贏
    is_win = False
    if contract['direction'] == 'LONG' and current_difficulty > 0.05: is_win = True
    elif contract['direction'] == 'SHORT' and current_difficulty <= 0.05: is_win = True
    
    try:
        tx_hex = ""
        
        if is_win:
            # User 贏: 構建部分簽名交易 (Oracle Signed)，存入 DB 等待用戶簽名
            user_addr_obj = PublicKey(contract['user_pubkey']).get_segwit_address()
            tx_hex = await build_win_path_partial_tx(contract, user_addr_obj)
            
            status = "WAITING_USER_SIG"
            msg = "Oracle signed. Transaction saved. Waiting for User signature."
            
            # 這裡不廣播，因為還沒簽完，只存入 DB
            db_update_status(contract['id'], status, tx_hex)
            
            # 通知前端有動作需要用戶執行
            await manager.broadcast({
                "type": "ACTION_REQUIRED",
                "contract_id": contract['id'],
                "status": status,
                "tx_hex": tx_hex,
                "message": msg
            })
            
            return {
                "result": status, 
                "tx_hex": tx_hex,
                "message": msg
            }

        else:
            # House 贏: 轉給 House (使用 LOSS 分支，後端全權處理)
            house_addr_obj = HOUSE_PRIV_KEY.get_public_key().get_segwit_address()
            tx_hex = await build_multisig_spend(contract, house_addr_obj)
            status = "SETTLED_LOSS"
            msg = "Oracle & House signed. Funds sent to House."
            
            txid = await broadcast_tx(tx_hex)
            
            if len(txid) != 64:
                 return {"result": "ERROR", "message": "Broadcast failed", "details": txid}

            db_update_status(contract['id'], status, tx_hex)
            
            # Broadcast Settle Event
            await manager.broadcast({
                "type": "SETTLED",
                "contract_id": contract['id'],
                "result": status,
                "txid": txid
            })

            return {
                "result": status, 
                "txid": txid, 
                "tx_hex": tx_hex,
                "message": msg
            }
            
    except ValueError as ve:
        # 特殊處理: 餘額不足 (通常是舊合約或未入金)
        if "no funds" in str(ve):
            print(f"Skipping contract {contract['id']}: No funds.")
            return {"result": "SKIPPED", "message": "No funds in contract address."}
        else:
            print(traceback.format_exc())
            return {"result": "ERROR", "error": str(ve)}

    except Exception as e:
        print(traceback.format_exc())
        return {"result": "ERROR", "error": str(e), "traceback": traceback.format_exc(), "message": "Settlement failed"}

@app.post("/api/settle")
async def settle_contract(req: SettleRequest):
    contract = db_get_contract(req.contract_id)
    if not contract: raise HTTPException(404, "Contract not found")
    return await execute_settlement(contract, req.current_difficulty)

@app.post("/api/settle_all")
async def settle_all_contracts(req: SettleAllRequest):
    # 1. Get all PENDING contracts
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM contracts WHERE status = 'PENDING'")
    contracts = [dict(row) for row in c.fetchall()]
    conn.close()

    results = []
    for contract in contracts:
        res = await execute_settlement(contract, req.current_difficulty)
        results.append({"id": contract['id'], "result": res})
            
    return {"summary": results, "count": len(results)}

@app.get("/api/last-block-time")
async def get_time_since_last_block():
    try:
        async with httpx.AsyncClient() as client:
            # 1. 獲取最新區塊列表 (通常第一個就是最新的)
            response = await client.get(f"https://mempool.space/signet/api/blocks")
            
            if response.status_code != 200:
                raise HTTPException(status_code=502, detail="無法連接外部 API")
            
            blocks = response.json()
            latest_block = blocks[0]
            
            # 2. 獲取時間戳
            last_block_time = latest_block['timestamp']
            
            # 3. 計算差距
            current_time = int(time.time())
            seconds_elapsed = current_time - last_block_time
            
            return {
                "network": "Bitcoin Signet",
                "block_height": latest_block['height'],
                "seconds_since_mined": seconds_elapsed,
                "formatted_time": f"{seconds_elapsed} seconds ago"
            }
            
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
