// Copyright 2026 Aayush Chawla
// SPDX-License-Identifier: Apache-2.0

// Shared chat send path — used by BOTH ChatSidebar and the full-page /chat
// route.
//
// A send is not a one-liner: optimistic user bubble, WS-first with a REST
// fallback, auto-created-conversation tracking, and an error bubble. Two
// copies of that would drift and the streaming behaviour of the two surfaces
// would diverge, so the page reuses this instead of duplicating the sidebar's
// send(). Extracted from ChatSidebar's send() so the sidebar is unchanged.
//
// The streaming side (chunks, tool events, done/error, WS-drop recovery) is
// NOT here — that lives once in $lib/stores/chatStream.ts and is shared by
// every surface already.

import { get } from 'svelte/store';
import { engineApi } from '$lib/api/engine';
import type { ChatMessage as ChatMessageType } from '$lib/api/types';
import {
	activeConversationId,
	chatCardContext,
	chatCardIds,
	chatMessages,
	chatSending,
	streamingMessageId
} from '$lib/stores/chat';
import { sendMessage, wsStatus } from '$lib/stores/websocket';

export interface ChatSendOptions {
	/**
	 * Optional assistant focus/persona id (e.g. 'coding'). It rides the chat
	 * request and is resolved server-side against a fixed map; an unknown id is
	 * ignored, so this is a selector and never arbitrary prompt text.
	 */
	focus?: string;
	/**
	 * Whether to attach the ambient card context (`chatCardContext` /
	 * `chatCardIds`) — the sidebar's "chat about this card" state. Defaults to
	 * true, so the sidebar behaves exactly as before.
	 *
	 * The full-page /chat passes false. Those stores are shared between the two
	 * surfaces, so without this the card the user asked the SIDEBAR about would
	 * ride along on every full-page turn; and because `card_ids` would ride
	 * along too, `_ensure_conversation` would persist that card set on a
	 * brand-new /chat conversation, making the card's later
	 * `GET /chat/conversations/by-cards` lookup open the /chat thread.
	 */
	attachCardContext?: boolean;
}

/** Outbound payload for one turn (WS `chat_message` payload / REST body). */
export interface ChatSendPayload {
	message: string;
	conversation_id: string | null;
	card_context?: string;
	card_ids?: string[];
	focus?: string;
}

/** True when the WS is connected (WS-first, REST fallback — see sendChatMessage). */
export function isWsConnected(): boolean {
	let connected = false;
	// wsStatus is a plain readable store, so subscribing once and dropping the
	// subscription is the synchronous "current value" read in a non-component
	// module (mirrors ChatSidebar.send()).
	const unsubscribe = wsStatus.subscribe((s) => (connected = s === 'connected'));
	unsubscribe();
	return connected;
}

/**
 * Build the outbound payload for one turn. Pure.
 *
 * Optional fields are omitted rather than sent as null/empty: the engine's
 * request model treats an absent card_context/card_ids/focus as "not set", and
 * an empty string would be injected into the system prompt as context.
 */
export function buildSendPayload(
	text: string,
	conversationId: string | null,
	cardContext: string | null,
	cardIds: string[] | null,
	focus?: string
): ChatSendPayload {
	return {
		message: text,
		conversation_id: conversationId,
		...(cardContext ? { card_context: cardContext } : {}),
		...(cardIds && cardIds.length > 0 ? { card_ids: cardIds } : {}),
		...(focus ? { focus } : {})
	};
}

/**
 * The optimistic user bubble shown before the server confirms. Pure; the temp
 * id is only ever used for keying, since the engine echoes back its own user
 * row on reload.
 */
export function buildOptimisticUserMessage(
	text: string,
	conversationId: string | null
): ChatMessageType {
	return {
		message_id: `tmp-${Date.now()}`,
		timestamp: new Date().toISOString(),
		role: 'user',
		content: text,
		referenced_cards: [],
		referenced_events: [],
		conversation_id: conversationId ?? undefined
	};
}

/** Error bubble shown when the REST fallback fails. */
function buildErrorMessage(): ChatMessageType {
	return {
		message_id: `err-${Date.now()}`,
		timestamp: new Date().toISOString(),
		role: 'assistant',
		content: 'Failed to send message. Please try again.',
		referenced_cards: [],
		referenced_events: []
	};
}

/**
 * Send one chat turn through the shared stores.
 *
 * Returns false when the turn was refused (empty text, or a reply is already
 * in flight) so callers can decide whether to clear their local input. On a
 * dispatched turn it returns true immediately — the reply itself arrives via
 * the module-level stream handler, not from this call.
 *
 * Busy is read from the stores rather than passed in: the old component-local
 * flag could stay stuck true forever after a lost stream, which silently
 * blocked every later send (see stores/chat.ts).
 */
export async function sendChatMessage(
	text: string,
	options: ChatSendOptions = {}
): Promise<boolean> {
	const trimmed = text.trim();
	const busy = get(chatSending) || get(streamingMessageId) !== null;
	if (!trimmed || busy) return false;

	const conversationId = get(activeConversationId);
	chatMessages.update((msgs) => [...msgs, buildOptimisticUserMessage(trimmed, conversationId)]);
	chatSending.set(true);

	// Card context is opt-out per send, not ambient to every surface: see
	// ChatSendOptions.attachCardContext.
	const attachCardContext = options.attachCardContext ?? true;
	const cardContext = attachCardContext ? get(chatCardContext) : null;
	const cardIds = attachCardContext ? get(chatCardIds) : null;
	const payload = buildSendPayload(trimmed, conversationId, cardContext, cardIds, options.focus);

	if (isWsConnected()) {
		sendMessage({ type: 'chat_message', payload });
		return true;
	}

	// REST fallback (WS down): no chunks, one final message.
	try {
		const resp = await engineApi.sendChat(
			trimmed,
			conversationId ?? undefined,
			cardContext ?? undefined,
			cardIds ?? undefined,
			options.focus
		);
		chatMessages.update((msgs) => [...msgs, resp.message]);
		// Track a conversation the backend auto-created for this first message.
		if (resp.message.conversation_id && !conversationId) {
			activeConversationId.set(resp.message.conversation_id);
		}
	} catch {
		chatMessages.update((msgs) => [...msgs, buildErrorMessage()]);
	} finally {
		chatSending.set(false);
	}
	return true;
}
