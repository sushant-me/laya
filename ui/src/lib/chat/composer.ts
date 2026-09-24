// Copyright 2026 Aayush Chawla
// SPDX-License-Identifier: Apache-2.0

// Pure composer logic for the chat surfaces (sidebar + full-page /chat route).
//
// These rules are extracted from the components so they can be unit-tested:
// they are the only non-trivial branching in the composer, and every one of
// them has a bug behind it (Enter-to-send firing mid-IME-composition, a send
// button that stays enabled while a reply streams, a "responding" state the
// user cannot see). No Svelte or store imports here — pure functions only.

/** What the composer should do with a keydown on the input. */
export type ComposerAction =
	/** Send the message (and swallow the Enter). */
	| 'send'
	/** Swallow the Enter without sending (send is currently not allowed). */
	| 'block'
	/** Leave the event alone so the browser's default applies (newline, IME). */
	| 'default';

export interface ComposerKeyInput {
	key: string;
	shiftKey: boolean;
	/** True while an IME candidate window is open (`event.isComposing`). */
	isComposing?: boolean;
	/** True while a reply is in flight (chatSending || streamingMessageId). */
	busy: boolean;
	/** Whether the trimmed input has anything to send. */
	hasText: boolean;
}

/**
 * Decide what an Enter keypress means.
 *
 * Shift+Enter must insert a newline, and an Enter pressed while an IME
 * composition is open must confirm the candidate rather than send the
 * half-composed text — both return 'default' so the browser handles them.
 *
 * While busy, or with an empty input, Enter is swallowed ('block') instead of
 * being passed through: the old sidebar always preventDefault()ed and then
 * early-returned from send(), so Enter never inserted a stray newline, and
 * matching that keeps the two surfaces behaving identically.
 */
export function resolveComposerAction(input: ComposerKeyInput): ComposerAction {
	if (input.key !== 'Enter') return 'default';
	// isComposing is checked before shiftKey: some IMEs report Shift+Enter.
	if (input.isComposing) return 'default';
	if (input.shiftKey) return 'default';
	if (input.busy || !input.hasText) return 'block';
	return 'send';
}

/**
 * Whether the send action is available. Busy wins over an empty input so the
 * button's disabled state is driven by one expression in both surfaces.
 */
export function canSubmit(text: string, busy: boolean): boolean {
	return !busy && text.trim().length > 0;
}

/**
 * Whether the composer should clear itself once a send resolves.
 *
 * Only the text that was actually dispatched may be discarded, and only while
 * the input still holds exactly that text. `sendChatMessage` awaits the whole
 * REST round trip on the WS-down fallback, so an unconditional clear (the
 * version this replaces) wiped text the user typed while a slow reply was in
 * flight. On the WS path the promise settles in a microtask, so in practice
 * the composer still clears immediately as it always did.
 */
export function shouldClearAfterSend(
	sentText: string,
	currentText: string,
	dispatched: boolean
): boolean {
	return dispatched && currentText === sentText;
}

/**
 * Visible status of an in-flight turn, or null when idle.
 *
 * The composer must *show* that it is disabled rather than only greying the
 * send button — a tool-heavy turn can run for a while with no visible output,
 * and a silently-disabled input reads as a broken app.
 */
export function pendingLabel(busy: boolean, activeTools: string[]): string | null {
	if (!busy) return null;
	if (activeTools.length > 0) return `Looking up: ${activeTools.join(', ')}`;
	return 'Responding…';
}
