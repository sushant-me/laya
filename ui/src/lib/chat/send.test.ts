// Copyright 2026 Aayush Chawla
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect, beforeEach, vi } from 'vitest';
import { get } from 'svelte/store';
import {
	activeConversationId,
	chatCardContext,
	chatCardIds,
	chatMessages,
	chatSending,
	streamingMessageId
} from '$lib/stores/chat';
import { sendMessage, wsStatus } from '$lib/stores/websocket';
import { engineApi } from '$lib/api/engine';
import type { ChatMessage } from '$lib/api/types';
import {
	buildOptimisticUserMessage,
	buildSendPayload,
	sendChatMessage
} from './send';

// The WS sender is the one side effect in sendChatMessage; mock only that so
// the test can assert on the outbound frame while every other store stays real.
vi.mock('$lib/stores/websocket', async (importOriginal) => {
	const actual = await importOriginal<typeof import('$lib/stores/websocket')>();
	return { ...actual, sendMessage: vi.fn() };
});

// The REST fallback is the other side effect. It was previously never exercised
// (every test drove the WS path), and that unexercised WS-down branch is exactly
// where the input-loss defect lived.
vi.mock('$lib/api/engine', () => ({ engineApi: { sendChat: vi.fn() } }));

function assistantMessage(conversationId = 'conv_1'): ChatMessage {
	return {
		message_id: 'msg_1',
		timestamp: '2026-01-01T00:00:00.000Z',
		role: 'assistant',
		content: 'done',
		referenced_cards: [],
		referenced_events: [],
		conversation_id: conversationId
	};
}

beforeEach(() => {
	vi.mocked(sendMessage).mockClear();
	vi.mocked(engineApi.sendChat).mockReset();
	chatMessages.set([]);
	chatSending.set(false);
	streamingMessageId.set(null);
	activeConversationId.set(null);
	chatCardContext.set(null);
	chatCardIds.set(null);
	wsStatus.set('disconnected');
});

describe('buildSendPayload', () => {
	it('omits card context, card ids and focus when they are unset', () => {
		// Absent fields must stay absent: the engine reads an empty string as
		// "there is context" and would inject it into the system prompt.
		expect(buildSendPayload('hello', null, null, null)).toEqual({
			message: 'hello',
			conversation_id: null
		});
	});

	it('omits an empty card-context string and an empty card-id list', () => {
		expect(buildSendPayload('hi', 'conv_1', '', [])).toEqual({
			message: 'hi',
			conversation_id: 'conv_1'
		});
	});

	it('carries card context and card ids when present', () => {
		expect(buildSendPayload('hi', 'conv_1', 'CARD DATA', ['card_a'])).toEqual({
			message: 'hi',
			conversation_id: 'conv_1',
			card_context: 'CARD DATA',
			card_ids: ['card_a']
		});
	});

	it('carries the focus only when one is given', () => {
		expect(buildSendPayload('hi', null, null, null, 'coding').focus).toBe('coding');
		expect(buildSendPayload('hi', null, null, null)).not.toHaveProperty('focus');
	});
});

describe('buildOptimisticUserMessage', () => {
	it('builds a user bubble with a temporary id', () => {
		const msg = buildOptimisticUserMessage('fix it', 'conv_1');
		expect(msg.role).toBe('user');
		expect(msg.content).toBe('fix it');
		expect(msg.conversation_id).toBe('conv_1');
		expect(msg.message_id.startsWith('tmp-')).toBe(true);
		expect(msg.referenced_cards).toEqual([]);
	});

	it('leaves conversation_id undefined for a brand-new chat', () => {
		// The store type has conversation_id?: string — null would fail the
		// ChatMessage contract and break the message key/stream matching.
		expect(buildOptimisticUserMessage('hi', null).conversation_id).toBeUndefined();
	});
});

describe('sendChatMessage', () => {
	it('refuses empty input without touching the stores', async () => {
		expect(await sendChatMessage('   ')).toBe(false);
		expect(get(chatMessages)).toEqual([]);
		expect(get(chatSending)).toBe(false);
		expect(sendMessage).not.toHaveBeenCalled();
	});

	it('refuses while a reply is already streaming', async () => {
		streamingMessageId.set('msg_live');
		expect(await sendChatMessage('second message')).toBe(false);
		expect(get(chatMessages)).toEqual([]);
		expect(sendMessage).not.toHaveBeenCalled();
	});

	it('appends the user bubble and sends over the WS when connected', async () => {
		wsStatus.set('connected');
		activeConversationId.set('conv_1');

		expect(await sendChatMessage('  fix the parser  ', { focus: 'coding' })).toBe(true);

		expect(sendMessage).toHaveBeenCalledTimes(1);
		expect(sendMessage).toHaveBeenCalledWith({
			type: 'chat_message',
			payload: { message: 'fix the parser', conversation_id: 'conv_1', focus: 'coding' }
		});
		const msgs = get(chatMessages);
		expect(msgs).toHaveLength(1);
		expect(msgs[0].content).toBe('fix the parser');
		// Busy is set here; the module-level stream handler clears it on
		// chat_stream_done — nothing in this call does.
		expect(get(chatSending)).toBe(true);
	});

	it('does not send a focus unless the caller opts in', async () => {
		wsStatus.set('connected');
		await sendChatMessage('plain question');
		const payload = vi.mocked(sendMessage).mock.calls[0][0] as {
			payload: Record<string, unknown>;
		};
		expect(payload.payload).not.toHaveProperty('focus');
	});

	it('sends card context through when it is set', async () => {
		wsStatus.set('connected');
		chatCardContext.set('CARD DATA');
		chatCardIds.set(['card_a', 'card_b']);

		await sendChatMessage('about these');

		const payload = vi.mocked(sendMessage).mock.calls[0][0] as {
			payload: Record<string, unknown>;
		};
		expect(payload.payload.card_context).toBe('CARD DATA');
		expect(payload.payload.card_ids).toEqual(['card_a', 'card_b']);
	});

	it('does not hit the REST fallback while the WS is connected', async () => {
		wsStatus.set('connected');
		await sendChatMessage('over the socket');
		expect(engineApi.sendChat).not.toHaveBeenCalled();
	});
});

describe('sendChatMessage REST fallback (WS down)', () => {
	it('awaits the round trip and appends the final assistant message', async () => {
		const final = assistantMessage('conv_rest');
		vi.mocked(engineApi.sendChat).mockResolvedValue({
			message: final,
			referenced_cards: [],
			referenced_events: []
		});

		expect(await sendChatMessage('  fix the parser  ', { focus: 'coding' })).toBe(true);

		expect(engineApi.sendChat).toHaveBeenCalledTimes(1);
		expect(vi.mocked(engineApi.sendChat).mock.calls[0]).toEqual([
			'fix the parser',
			undefined,
			undefined,
			undefined,
			'coding'
		]);
		// Optimistic user bubble + the single final reply (no chunks on REST).
		const msgs = get(chatMessages);
		expect(msgs).toHaveLength(2);
		expect(msgs[1]).toEqual(final);
		// The REST reply IS the conversation; a brand-new chat adopts its id.
		expect(get(activeConversationId)).toBe('conv_rest');
		// finally{} cleared the busy flag — nothing else does on this path.
		expect(get(chatSending)).toBe(false);
		expect(sendMessage).not.toHaveBeenCalled();
	});

	it('passes the existing conversation id instead of adopting a new one', async () => {
		activeConversationId.set('conv_existing');
		vi.mocked(engineApi.sendChat).mockResolvedValue({
			message: assistantMessage('conv_existing'),
			referenced_cards: [],
			referenced_events: []
		});

		await sendChatMessage('continue');

		expect(vi.mocked(engineApi.sendChat).mock.calls[0][1]).toBe('conv_existing');
		expect(get(activeConversationId)).toBe('conv_existing');
	});

	it('shows an error bubble and still clears busy when the REST call fails', async () => {
		vi.mocked(engineApi.sendChat).mockRejectedValue(new Error('engine down'));

		expect(await sendChatMessage('will fail')).toBe(true);

		const msgs = get(chatMessages);
		expect(msgs).toHaveLength(2);
		expect(msgs[1].role).toBe('assistant');
		expect(msgs[1].content).toBe('Failed to send message. Please try again.');
		expect(get(chatSending)).toBe(false);
	});
});

describe('card-context scoping (full-page /chat opts out)', () => {
	it('attaches ambient card context by default (sidebar) over the WS', async () => {
		wsStatus.set('connected');
		chatCardContext.set('CARD DATA');
		chatCardIds.set(['card_a']);

		await sendChatMessage('about this card');

		const payload = vi.mocked(sendMessage).mock.calls[0][0] as {
			payload: Record<string, unknown>;
		};
		expect(payload.payload.card_context).toBe('CARD DATA');
		expect(payload.payload.card_ids).toEqual(['card_a']);
	});

	it('omits ambient card context over the WS when attachCardContext is false', async () => {
		// The stale shared store is set (the user asked the sidebar about a card)
		// but the full-page chat must not carry it.
		wsStatus.set('connected');
		chatCardContext.set('CARD DATA');
		chatCardIds.set(['card_a']);

		await sendChatMessage('unrelated full-page turn', {
			focus: 'coding',
			attachCardContext: false
		});

		const payload = vi.mocked(sendMessage).mock.calls[0][0] as {
			payload: Record<string, unknown>;
		};
		expect(payload.payload).not.toHaveProperty('card_context');
		// card_ids is what _ensure_conversation would PERSIST on the new
		// conversation, so its absence is what keeps by-cards from opening it.
		expect(payload.payload).not.toHaveProperty('card_ids');
		expect(payload.payload.focus).toBe('coding');
	});

	it('omits ambient card context on the REST fallback too when attachCardContext is false', async () => {
		chatCardContext.set('CARD DATA');
		chatCardIds.set(['card_a']);
		vi.mocked(engineApi.sendChat).mockResolvedValue({
			message: assistantMessage('conv_rest'),
			referenced_cards: [],
			referenced_events: []
		});

		await sendChatMessage('unrelated full-page turn', {
			focus: 'coding',
			attachCardContext: false
		});

		expect(vi.mocked(engineApi.sendChat).mock.calls[0]).toEqual([
			'unrelated full-page turn',
			undefined,
			undefined,
			undefined,
			'coding'
		]);
	});
});
