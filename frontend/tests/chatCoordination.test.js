import assert from 'node:assert/strict'
import test from 'node:test'

import { ChatCoordination } from '../src/services/chatCoordination.js'

const THREAD = '11111111-1111-4111-8111-111111111111'
const OPERATION = '22222222-2222-4222-8222-222222222222'

class MemoryBroadcastChannel {
  static channels = new Map()

  constructor(name) {
    this.name = name
    this.onmessage = null
    const peers = MemoryBroadcastChannel.channels.get(name) || new Set()
    peers.add(this)
    MemoryBroadcastChannel.channels.set(name, peers)
  }

  postMessage(message) {
    for (const peer of MemoryBroadcastChannel.channels.get(this.name) || []) {
      if (peer !== this) peer.onmessage?.({ data: structuredClone(message) })
    }
  }

  close() {
    MemoryBroadcastChannel.channels.get(this.name)?.delete(this)
  }
}

function coordinator(tabId) {
  return new ChatCoordination({
    BroadcastChannelCtor: MemoryBroadcastChannel,
    uuidFactory: () => tabId,
    wait: async () => {},
    now: () => 1000,
  })
}

test('simultaneous confirm claims elect one advisory winner', async () => {
  MemoryBroadcastChannel.channels.clear()
  const first = coordinator('11111111-1111-4111-8111-111111111111')
  const second = coordinator('22222222-2222-4222-8222-222222222222')
  first.open('7', () => {})
  second.open('7', () => {})

  const [firstWon, secondWon] = await Promise.all([
    first.claimConfirm(THREAD, OPERATION),
    second.claimConfirm(THREAD, OPERATION),
  ])

  assert.deepEqual([firstWon, secondWon], [true, false])
  first.close()
  second.close()
})

test('state change notification carries only scope hints and makes peer reread', () => {
  MemoryBroadcastChannel.channels.clear()
  const first = coordinator('11111111-1111-4111-8111-111111111111')
  const second = coordinator('22222222-2222-4222-8222-222222222222')
  const received = []
  first.open('7', () => {})
  second.open('7', (event) => received.push(event))

  first.notifyStateChanged(THREAD, OPERATION)

  assert.equal(received.length, 1)
  assert.equal(received[0].type, 'state_changed')
  assert.equal(received[0].thread_id, THREAD)
  assert.equal(received[0].operation_id, OPERATION)
  assert.equal('result' in received[0], false)
  assert.equal('payload' in received[0], false)
  assert.equal('token' in received[0], false)
  first.close()
  second.close()
})

test('messages are isolated by authenticated actor channel', () => {
  MemoryBroadcastChannel.channels.clear()
  const first = coordinator('11111111-1111-4111-8111-111111111111')
  const second = coordinator('22222222-2222-4222-8222-222222222222')
  let received = 0
  first.open('7', () => {})
  second.open('8', () => { received += 1 })

  first.notifyStateChanged(THREAD, OPERATION)

  assert.equal(received, 0)
  first.close()
  second.close()
})
