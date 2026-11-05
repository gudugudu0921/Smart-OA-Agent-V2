const CHANNEL_PREFIX = 'smart_oa_chat_coordination_v1'
const CLAIM_WINDOW_MS = 120
const CLAIM_TTL_MS = 10_000

function required(value, field) {
  const normalized = String(value ?? '').trim()
  if (!normalized) throw new TypeError(`${field} is required`)
  return normalized
}

export class ChatCoordination {
  constructor({
    BroadcastChannelCtor = globalThis.BroadcastChannel,
    uuidFactory = () => globalThis.crypto.randomUUID(),
    now = () => Date.now(),
    wait = (delay) => new Promise((resolve) => setTimeout(resolve, delay)),
  } = {}) {
    this.BroadcastChannelCtor = BroadcastChannelCtor
    this.uuidFactory = uuidFactory
    this.now = now
    this.wait = wait
    this.tabId = uuidFactory()
    this.actorId = null
    this.channel = null
    this.listener = null
    this.claims = new Map()
  }

  open(actorId, listener) {
    const normalizedActor = required(actorId, 'actorId')
    if (this.actorId === normalizedActor && this.channel) {
      this.listener = listener
      return
    }
    this.close()
    this.actorId = normalizedActor
    this.listener = listener
    if (typeof this.BroadcastChannelCtor !== 'function') return
    this.channel = new this.BroadcastChannelCtor(`${CHANNEL_PREFIX}:${normalizedActor}`)
    this.channel.onmessage = (event) => this.receive(event?.data)
  }

  receive(message) {
    if (!message || typeof message !== 'object') return
    if (message.actor_id !== this.actorId || message.sender_id === this.tabId) return
    if (!['confirm_claim', 'state_changed'].includes(message.type)) return
    if (!message.thread_id || !message.operation_id) return
    if (message.type === 'confirm_claim') {
      const key = `${message.thread_id}:${message.operation_id}`
      const claims = this.claims.get(key) || new Map()
      claims.set(message.sender_id, Number(message.timestamp) || this.now())
      this.claims.set(key, claims)
    }
    this.listener?.({ ...message })
  }

  post(type, threadId, operationId) {
    if (!this.channel || !this.actorId) return
    this.channel.postMessage({
      type,
      actor_id: this.actorId,
      thread_id: required(threadId, 'threadId'),
      operation_id: required(operationId, 'operationId'),
      sender_id: this.tabId,
      timestamp: this.now(),
    })
  }

  async claimConfirm(threadId, operationId) {
    const normalizedThread = required(threadId, 'threadId')
    const normalizedOperation = required(operationId, 'operationId')
    if (!this.channel) return true
    const key = `${normalizedThread}:${normalizedOperation}`
    const timestamp = this.now()
    const claims = this.claims.get(key) || new Map()
    claims.set(this.tabId, timestamp)
    this.claims.set(key, claims)
    this.post('confirm_claim', normalizedThread, normalizedOperation)
    await this.wait(CLAIM_WINDOW_MS)
    const cutoff = this.now() - CLAIM_TTL_MS
    const contenders = [...(this.claims.get(key) || new Map()).entries()]
      .filter(([, claimedAt]) => claimedAt >= cutoff)
      .map(([tabId]) => tabId)
      .sort()
    return contenders[0] === this.tabId
  }

  notifyStateChanged(threadId, operationId) {
    this.post('state_changed', threadId, operationId)
  }

  close() {
    if (this.channel) {
      this.channel.onmessage = null
      this.channel.close()
    }
    this.channel = null
    this.actorId = null
    this.listener = null
    this.claims.clear()
  }
}

export const createChatCoordination = (options) => new ChatCoordination(options)
