<script setup>
import { ref, reactive, onMounted, markRaw } from 'vue'

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
  
  try {
    log('正在請求錢包簽名...')
    // 注意: Unisat/OKX 通常需要 PSBT。這裡我們嘗試直接簽名或提示用戶。
    // 由於後端回傳的是 Raw Hex (部分簽名)，若錢包不支援直接簽 Raw Hex，
    // 則需要前端引入 bitcoinjs-lib 將其包裝成 PSBT。
    // 這裡為了演示流程，我們先顯示 Hex。
    
    console.log("Partial TX Hex:", currentContract.tx_hex)
    
    if (wallet.name.includes('UniSat')) {
        try {
            // 嘗試使用 signPsbt (需要先轉 PSBT，這裡可能會失敗如果格式不對)
            // 或是 signMessage (不適用)
            alert("請複製 Console 中的 Hex，使用支援 Raw Taproot 簽名的工具完成簽名並廣播。")
        } catch (e) {
            log("簽名請求失敗: " + e.message)
        }
    } else {
        alert("請複製 Console 中的 Hex，使用支援 Raw Taproot 簽名的工具完成簽名並廣播。")
    }
    
    log(`待簽名 Hex 已輸出至 Console`)
    
  } catch (e) {
    log(`操作失敗: ${e.message}`)
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
</style>
