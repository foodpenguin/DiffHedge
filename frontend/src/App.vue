<script setup>
import { ref, reactive, onMounted, markRaw } from 'vue'
import { Buffer } from 'buffer'
import * as bitcoin from 'bitcoinjs-lib'
import * as ecc from 'tiny-secp256k1'

// Initialize bitcoinjs-lib
bitcoin.initEccLib(ecc)
window.Buffer = Buffer

const API_BASE = 'http://localhost:8000/api'

// Non-reactive provider storage to avoid Proxy issues with Wallet extensions
let currentProvider = null

// State
const wallet = reactive({
  connected: false,
  address: '',
  publicKey: '',
  name: ''
})

const stats = reactive({
  difficulty: 0,
  hashprice: 0,
  houseAddress: ''
})

const contractForm = reactive({
  amount: 1000,
  direction: 'LONG'
})

const currentContract = reactive({
  id: null,
  address: '',
  status: '',
  tx_hex: '',
  logs: []
})

const userContracts = ref([])

const settleForm = reactive({
  difficulty: 0.06
})

// Helper to log messages
const log = (msg) => {
  const timestamp = new Date().toLocaleTimeString()
  currentContract.logs.unshift(`[${timestamp}] ${msg}`)
}

// WebSocket Setup
onMounted(() => {
  const ws = new WebSocket('ws://localhost:8000/ws')
  
  ws.onopen = () => {
    log('[WS] WebSocket 連線已建立')
  }
  
  ws.onmessage = (event) => {
    const data = JSON.parse(event.data)
    log(`[WS] 收到通知: ${data.type} - ID: ${data.contract_id}`)
    
    // 如果是當前關注的合約，更新狀態
    if (currentContract.id && data.contract_id === currentContract.id) {
      if (data.type === 'MATCHED') {
        currentContract.status = 'MATCHED'
        log('House 已跟單！合約生效中。')
      } else if (data.type === 'SETTLED') {
        currentContract.status = data.result
        log(`合約已結算: ${data.result}`)
      } else if (data.type === 'ACTION_REQUIRED') {
        currentContract.status = data.status
        currentContract.tx_hex = data.tx_hex
        log(`[需行動] ${data.message}`)
      }
    }
  }
  
  ws.onerror = (error) => {
    console.error('WebSocket Error:', error)
  }
})

// 1. Connect Wallet
const connectWallet = async () => {
  try {
    let provider = null
    let name = ''

    if (typeof window.unisat !== 'undefined') {
      provider = window.unisat
      name = 'UniSat Wallet'
      try {
        const net = await provider.getNetwork()
        if (net !== 'testnet') {
          await provider.switchNetwork("testnet")
        }
      } catch (e) {
        log("切換網路失敗 (UniSat): " + e.message)
      }
    } else if (typeof window.okxwallet !== 'undefined' && window.okxwallet.bitcoin) {
      provider = window.okxwallet.bitcoin
      name = 'OKX Wallet'
      try {
        await provider.switchNetwork("testnet")
      } catch (e) {
        log("切換網路失敗 (OKX): " + e.message)
      }
    } else {
      alert('請安裝 OKX Wallet 或 UniSat Wallet!')
      return
    }

    const accounts = await provider.requestAccounts()
    wallet.address = accounts[0]
    wallet.publicKey = await provider.getPublicKey()
    currentProvider = provider
    wallet.name = name
    wallet.connected = true
    
    log(`已連接 ${name}: ${wallet.address}`)

    if (wallet.address.startsWith('bc1')) {
      log("⚠️ 警告: 偵測到 Mainnet 地址! 請手動切換錢包至 Testnet/Signet")
      alert("您似乎連接到了比特幣主網 (Mainnet)。本應用程式僅在 Signet 測試網運行。請在錢包設定中切換網路。")
    }

    fetchStats()
    fetchUserContracts()
  } catch (e) {
    log(`連接失敗: ${e.message}`)
  }
}

// 2. Fetch Stats
const fetchStats = async () => {
  try {
    const res = await fetch(`${API_BASE}/stats`)
    const data = await res.json()
    stats.difficulty = data.difficulty
    stats.hashprice = data.hashprice_sats
    stats.houseAddress = data.house_address
    log('已更新系統狀態')
  } catch (e) {
    log(`獲取狀態失敗: ${e.message}`)
  }
}

// 3. Create & Deposit & Match (Atomic Flow)
const createAndDeposit = async () => {
  if (!wallet.connected) return alert('請先連接錢包')
  
  try {
    // Step 1: Create Contract
    log('1. 正在建立合約...')
    const res = await fetch(`${API_BASE}/create_contract`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        user_pubkey: wallet.publicKey,
        amount: contractForm.amount,
        direction: contractForm.direction
      })
    })
    const data = await res.json()
    
    if (data.status !== 'success') {
      log(`建立失敗: ${JSON.stringify(data)}`)
      return
    }

    currentContract.id = data.contract_id
    currentContract.address = data.deposit_address
    currentContract.status = 'CREATED'
    log(`合約建立成功! ID: ${data.contract_id}`)
    log(`請入金到: ${data.deposit_address}`)

    // Step 2: Deposit (Sign & Send)
    log(`2. 正在呼叫錢包發送 ${contractForm.amount} sats...`)
    const txid = await currentProvider.sendBitcoin(currentContract.address, parseInt(contractForm.amount), {feeRate: 1.5})
    log(`入金交易已廣播! TXID: ${txid}`)

    // Step 3: Auto Match (Wait 3s for propagation)
    log('3. 等待交易傳播後自動觸發 Match (3秒)...')
    setTimeout(async () => {
      await matchContract()
    }, 3000)

  } catch (e) {
    log(`流程錯誤: ${e.message}`)
    if (currentContract.id) {
      log('交易失敗，正在清理合約...')
      try {
        await fetch(`${API_BASE}/cancel_contract`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ contract_id: currentContract.id })
        })
        log(`合約 ID ${currentContract.id} 已刪除`)
      } catch (cleanupError) {
        log(`清理合約失敗: ${cleanupError.message}`)
      }
      currentContract.id = null
      currentContract.address = ''
      currentContract.status = ''
    }
  }
}

// 5. Match (House Deposit)
const matchContract = async () => {
  if (!currentContract.id) return
  
  try {
    log('請求 House 跟單...')
    const res = await fetch(`${API_BASE}/match`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ contract_id: currentContract.id })
    })
    const data = await res.json()
    log(`Match 結果: ${JSON.stringify(data)}`)
  } catch (e) {
    log(`Match 請求錯誤: ${e.message}`)
  }
}

// 6. Settle All
const settleAllContracts = async () => {
  try {
    log(`請求批量結算 (難度: ${settleForm.difficulty})...`)
    const res = await fetch(`${API_BASE}/settle_all`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ 
        current_difficulty: settleForm.difficulty
      })
    })
    const data = await res.json()
    log(`批量結算結果: ${JSON.stringify(data)}`)
  } catch (e) {
    log(`結算請求錯誤: ${e.message}`)
  }
}

// 7. Refund
const refundContract = async () => {
  if (!currentContract.id) return
  
  try {
    log('請求退款...')
    const res = await fetch(`${API_BASE}/refund`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ contract_id: currentContract.id })
    })
    const data = await res.json()
    log(`退款結果: ${JSON.stringify(data)}`)
  } catch (e) {
    log(`退款請求錯誤: ${e.message}`)
  }
}

// 8. Check Status
const checkStatus = async () => {
  if (!currentContract.id) return
  try {
    const res = await fetch(`${API_BASE}/contract/${currentContract.id}`)
    const data = await res.json()
    log(`合約狀態: ${JSON.stringify(data)}`)
  } catch (e) {
    log(`查詢失敗: ${e.message}`)
  }
}

// 9. Sign & Broadcast (User Win)
const signAndBroadcast = async () => {
  if (!currentContract.tx_hex) return alert('No transaction to sign')
  if (!currentContract.address) return alert('Contract address missing')
  
  try {
    log('正在請求錢包簽名...')
    console.log("Contract Address:", currentContract.address)
    
    // 1. Parse the partial transaction
    const tx = bitcoin.Transaction.fromHex(currentContract.tx_hex)
    
    // 2. Extract Witness Data
    const witness = tx.ins[0].witness
    // Expecting: [SigOrPlaceholder, SigOrPlaceholder, Script, ControlBlock]
    const script = witness[witness.length - 2]
    const controlBlock = witness[witness.length - 1]
    
    // 3. Construct PSBT for signing
    const psbt = new bitcoin.Psbt({ network: bitcoin.networks.testnet })
    
    const value = BigInt(currentContract.amount * 2)
    // Ensure address is valid for testnet
    let scriptPubKey
    try {
        scriptPubKey = bitcoin.address.toOutputScript(currentContract.address, bitcoin.networks.testnet)
    } catch (err) {
        console.error("Address Error:", err)
        throw new Error(`Invalid address for Testnet: ${currentContract.address}`)
    }
    
    psbt.addInput({
        hash: tx.ins[0].hash,
        index: tx.ins[0].index,
        witnessUtxo: {
            script: scriptPubKey,
            value: value
        },
        tapLeafScript: [{
            leafVersion: 0xc0,
            script: script,
            controlBlock: controlBlock
        }],
        sighashType: bitcoin.Transaction.SIGHASH_DEFAULT
    })
    
    psbt.addOutput({
        address: bitcoin.address.fromOutputScript(tx.outs[0].script, bitcoin.networks.testnet),
        value: tx.outs[0].value
    })
    
    // 4. Request Signature
    const psbtHex = psbt.toHex()
    const signedPsbtHex = await window.unisat.signPsbt(psbtHex)
    
    // 5. Extract Signature
    const signedPsbt = bitcoin.Psbt.fromHex(signedPsbtHex)
    const tapScriptSig = signedPsbt.data.inputs[0].tapScriptSig
    
    if (!tapScriptSig || tapScriptSig.length === 0) {
        throw new Error("No signature returned from wallet")
    }
    
    const userSig = tapScriptSig[0].signature
    
    // 6. Insert Signature into Witness Stack
    let inserted = false
    for (let i = 0; i < witness.length - 2; i++) {
        if (witness[i].length === 0) {
            witness[i] = userSig
            inserted = true
            break
        }
    }
    
    if (!inserted) {
        throw new Error("Could not find placeholder for signature in witness stack")
    }
    
    // 7. Broadcast
    const finalTxHex = tx.toHex()
    log("簽名完成，正在廣播...")
    
    const txid = await window.unisat.pushTx(finalTxHex)
    log(`廣播成功! TXID: ${txid}`)
    
    currentContract.status = 'SETTLED_WIN'
    
  } catch (e) {
    log(`簽名/廣播失敗: ${e.message}`)
    console.error(e)
  }
}

// 10. Fetch User Contracts
const fetchUserContracts = async () => {
  if (!wallet.publicKey) return
  try {
    const res = await fetch(`${API_BASE}/contracts/user/${wallet.publicKey}`)
    const data = await res.json()
    userContracts.value = data.contracts
    log(`已載入 ${data.count} 筆歷史合約`)
  } catch (e) {
    log(`載入歷史合約失敗: ${e.message}`)
  }
}

// 11. Claim All
const claimAll = async () => {
  if (!wallet.publicKey) return
  try {
    log('正在請求批量領取...')
    const res = await fetch(`${API_BASE}/claim_all`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user_pubkey: wallet.publicKey })
    })
    const data = await res.json()
    
    if (data.status === 'error') {
        log(`批量領取失敗: ${data.message}`)
        return
    }
    
    log(`批量交易已建立! 正在組裝 PSBT...`)
    console.log("Unsigned TX Hex:", data.unsigned_tx_hex)
    
    if (!data.unsigned_tx_hex) {
        throw new Error("Backend returned empty transaction hex")
    }

    // 1. 從未簽名交易建立 PSBT
    const tx = bitcoin.Transaction.fromHex(data.unsigned_tx_hex)
    const psbt = new bitcoin.Psbt({ network: bitcoin.networks.testnet })
    
    // 2. 根據後端指示加入 Inputs
    for (const inputData of data.psbt_inputs) {
        const i = inputData.index
        
        // A. 加入基本 Input 資訊
        psbt.addInput({
            hash: tx.ins[i].hash,
            index: tx.ins[i].index,
            witnessUtxo: {
                script: Buffer.from(inputData.scriptPubKey, 'hex'),
                value: BigInt(inputData.value)
            },
            tapLeafScript: [{
                leafVersion: inputData.tapLeafScript.leafVersion,
                script: Buffer.from(inputData.tapLeafScript.script, 'hex'),
                controlBlock: Buffer.from(inputData.tapLeafScript.controlBlock, 'hex')
            }]
        })
        
        // B. 加入 Oracle 的簽名 (TapScriptSig)
        // 注意: 這裡我們直接把 Oracle 簽名放進去，Unisat 簽名時會保留它
        psbt.updateInput(i, {
            tapScriptSig: [{
                pubkey: Buffer.from(inputData.oracleSig.pubkey, 'hex'),
                signature: Buffer.from(inputData.oracleSig.signature, 'hex'),
                leafHash: Buffer.from(inputData.oracleSig.leafHash, 'hex')
            }]
        })
    }
    
    // 3. 加入 Outputs
    tx.outs.forEach(out => {
        psbt.addOutput({
            script: out.script,
            value: out.value
        })
    })
    
    log("PSBT 組裝完成，請求錢包簽名...")
    
    // 4. 請求 Unisat 簽名 (用戶簽名)
    const psbtHex = psbt.toHex()
    const signedPsbtHex = await window.unisat.signPsbt(psbtHex)
    
    // 5. 廣播 (Unisat signPsbt 回傳的通常是已簽名的 PSBT Hex)
    // 我們需要提取最終交易並廣播
    const signedPsbt = bitcoin.Psbt.fromHex(signedPsbtHex)
    
    // 嘗試 Finalize (合併簽名)
    // 由於我們已經提供了 Oracle 簽名，且 Unisat 提供了 User 簽名，
    // bitcoinjs-lib 應該能自動完成 Finalize
    signedPsbt.finalizeAllInputs()
    
    const finalTx = signedPsbt.extractTransaction()
    const finalTxHex = finalTx.toHex()
    
    log("簽名完成，正在廣播...")
    const txid = await window.unisat.pushTx(finalTxHex)
    log(`批量領取成功! TXID: ${txid}`)
    
  } catch (e) {
    log(`批量領取失敗: ${e.message}`)
    console.error(e)
  }
}

</script>

<template>
  <div class="container">
    <h1>HashHedge 測試面板</h1>
    
    <!-- 1. Wallet Section -->
    <div class="card">
      <h2>1. 錢包連接</h2>
      <div v-if="!wallet.connected">
        <button @click="connectWallet">連接 OKX / UniSat Wallet</button>
      </div>
      <div v-else>
        <p><strong>錢包:</strong> {{ wallet.name }}</p>
        <p><strong>地址:</strong> {{ wallet.address }}</p>
        <p><strong>公鑰:</strong> {{ wallet.publicKey.substring(0, 20) }}...</p>
        <hr>
        <p><strong>系統難度:</strong> {{ stats.difficulty }}</p>
        <p><strong>House 地址:</strong> <span style="word-break: break-all;">{{ stats.houseAddress }}</span></p>
      </div>
    </div>

    <!-- 2. Create Contract -->
    <div class="card" :class="{ disabled: !wallet.connected }">
      <h2>2. 建立合約</h2>
      <div class="form-group">
        <label>金額 (sats):</label>
        <input type="number" v-model="contractForm.amount">
      </div>
      <div class="form-group">
        <label>方向:</label>
        <select v-model="contractForm.direction">
          <option value="LONG">做多 (LONG)</option>
          <option value="SHORT">做空 (SHORT)</option>
        </select>
      </div>
      <button @click="createAndDeposit" :disabled="!wallet.connected">建立合約並入金 (Create & Deposit)</button>
    </div>

    <!-- 3. Actions -->
    <div class="card" :class="{ disabled: !currentContract.id }">
      <h2>3. 合約操作 (ID: {{ currentContract.id || '未建立' }})</h2>
      <p v-if="currentContract.address" class="address-box">
        多簽地址: {{ currentContract.address }}
      </p>
      
      <div class="actions">
        <!-- <button @click="deposit" class="btn-primary">A. 錢包入金</button> -->
        <button @click="matchContract" class="btn-warning">手動觸發 Match</button>
        
        <div class="settle-group">
          <input type="number" step="0.01" v-model="settleForm.difficulty" placeholder="結算難度">
          <button @click="settleAllContracts" class="btn-success">批量結算 (Settle All)</button>
        </div>
        
        <div v-if="currentContract.status === 'WAITING_USER_SIG' || currentContract.status === 'WAITING_USER_SIG_REFUND'" class="user-action-group">
            <p style="color: red; font-weight: bold;">需要您的簽名！</p>
            <button @click="signAndBroadcast" class="btn-primary">簽名並廣播 (Sign & Broadcast)</button>
        </div>

        <button @click="refundContract" class="btn-danger">退款 (Refund)</button>
        <button @click="checkStatus" class="btn-info">查詢狀態</button>
      </div>
    </div>

    <!-- 4. History & Batch Claim -->
    <div class="card" v-if="wallet.connected">
      <h2>4. 歷史合約與批量領取</h2>
      <button @click="fetchUserContracts" class="btn-info">重新整理列表</button>
      <button @click="claimAll" class="btn-success">批量領取所有獲勝合約 (Claim All)</button>
      
      <div class="history-list">
        <div v-for="c in userContracts" :key="c.id" class="history-item">
            <span>ID: {{ c.id }}</span>
            <span>{{ c.direction }}</span>
            <span>{{ c.amount }} sats</span>
            <span :class="'status-' + c.status">{{ c.status }}</span>
            <button v-if="c.status === 'WAITING_USER_SIG'" @click="() => { currentContract.id = c.id; currentContract.tx_hex = c.tx_hex; currentContract.status = c.status; signAndBroadcast() }">簽名</button>
        </div>
      </div>
    </div>

    <!-- Logs -->
    <div class="card logs">
      <h2>操作日誌</h2>
      <div class="log-window">
        <div v-for="(msg, index) in currentContract.logs" :key="index" class="log-entry">
          {{ msg }}
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.container {
  max-width: 800px;
  margin: 0 auto;
  padding: 20px;
  font-family: sans-serif;
}
.card {
  border: 1px solid #ddd;
  padding: 20px;
  margin-bottom: 20px;
  border-radius: 8px;
  background: #f9f9f9;
}
.disabled {
  opacity: 0.6;
  pointer-events: none;
}
.form-group {
  margin-bottom: 10px;
}
input, select {
  padding: 8px;
  margin-left: 10px;
}
button {
  padding: 10px 20px;
  cursor: pointer;
  margin-right: 10px;
  margin-bottom: 10px;
}
.address-box {
  background: #eee;
  padding: 10px;
  word-break: break-all;
  font-family: monospace;
}
.log-window {
  background: #333;
  color: #0f0;
  padding: 10px;
  height: 200px;
  overflow-y: auto;
  font-family: monospace;
  font-size: 12px;
}
.btn-primary { background-color: #007bff; color: white; border: none; }
.btn-warning { background-color: #ffc107; border: none; }
.btn-success { background-color: #28a745; color: white; border: none; }
.btn-danger { background-color: #dc3545; color: white; border: none; }
.btn-info { background-color: #17a2b8; color: white; border: none; }

.history-list {
    margin-top: 15px;
    max-height: 300px;
    overflow-y: auto;
}
.history-item {
    display: flex;
    justify-content: space-between;
    padding: 8px;
    border-bottom: 1px solid #eee;
    align-items: center;
}
.status-PENDING { color: orange; }
.status-MATCHED { color: blue; }
.status-SETTLED_LOSS { color: red; }
.status-WAITING_USER_SIG { color: green; font-weight: bold; }
.status-SETTLED_WIN { color: green; }
</style>
