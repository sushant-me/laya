// Copyright 2026 Aayush Chawla
// SPDX-License-Identifier: Apache-2.0

import { describe, it, expect } from 'vitest';
import { canSubmit, pendingLabel, resolveComposerAction, shouldClearAfterSend } from './composer';

describe('resolveComposerAction', () => {
	it('sends on a plain Enter when there is text and nothing is in flight', () => {
		expect(
			resolveComposerAction({ key: 'Enter', shiftKey: false, busy: false, hasText: true })
		).toBe('send');
	});

	it('leaves Shift+Enter to the browser so it inserts a newline', () => {
		expect(
			resolveComposerAction({ key: 'Enter', shiftKey: true, busy: false, hasText: true })
		).toBe('default');
	});

	it('does not send while a reply is streaming, but still swallows the Enter', () => {
		// Swallowing matters: passing Enter through while busy would insert a
		// stray newline into the composer instead of doing nothing.
		expect(
			resolveComposerAction({ key: 'Enter', shiftKey: false, busy: true, hasText: true })
		).toBe('block');
	});

	it('does not send an empty or whitespace-only message', () => {
		expect(
			resolveComposerAction({ key: 'Enter', shiftKey: false, busy: false, hasText: false })
		).toBe('block');
	});

	it('leaves Enter to an open IME composition', () => {
		// Some IMEs report Shift+Enter as part of composition; the composing
		// check has to win or a half-composed message gets sent.
		expect(
			resolveComposerAction({
				key: 'Enter',
				shiftKey: true,
				isComposing: true,
				busy: false,
				hasText: true
			})
		).toBe('default');
		expect(
			resolveComposerAction({
				key: 'Enter',
				shiftKey: false,
				isComposing: true,
				busy: false,
				hasText: true
			})
		).toBe('default');
	});

	it('ignores every other key', () => {
		for (const key of ['a', 'Escape', 'Tab', 'ArrowUp']) {
			expect(resolveComposerAction({ key, shiftKey: false, busy: false, hasText: true })).toBe(
				'default'
			);
		}
	});
});

describe('canSubmit', () => {
	it('is true only with non-blank text and no reply in flight', () => {
		expect(canSubmit('fix the parser', false)).toBe(true);
		expect(canSubmit('   ', false)).toBe(false);
		expect(canSubmit('', false)).toBe(false);
		expect(canSubmit('fix the parser', true)).toBe(false);
	});
});

describe('pendingLabel', () => {
	it('is null when idle, so the composer shows the keyboard hint instead', () => {
		expect(pendingLabel(false, [])).toBeNull();
		expect(pendingLabel(false, ['search_cards'])).toBeNull();
	});

	it('names the tools being called while they run', () => {
		expect(pendingLabel(true, ['search_cards', 'get_card'])).toBe(
			'Looking up: search_cards, get_card'
		);
	});

	it('falls back to a generic label while waiting for the first chunk', () => {
		expect(pendingLabel(true, [])).toBe('Responding…');
	});
});

describe('shouldClearAfterSend', () => {
	it('clears when the turn was dispatched and the input is untouched', () => {
		expect(shouldClearAfterSend('fix the parser', 'fix the parser', true)).toBe(true);
	});

	it('does NOT clear text the user typed while a slow send was in flight', () => {
		// The regression: on the WS-down REST fallback, sendChatMessage awaits the
		// whole round trip before resolving while the sidebar textarea stays
		// enabled, so anything typed in that window must survive.
		expect(shouldClearAfterSend('fix the parser', 'fix the parser and the lexer', true)).toBe(
			false
		);
	});

	it('does not clear when the turn was refused (empty input or busy)', () => {
		// sendChatMessage returns false for an empty send or a reply already in
		// flight; those paths must leave the composer alone.
		expect(shouldClearAfterSend('   ', '   ', false)).toBe(false);
		expect(shouldClearAfterSend('second', 'second', false)).toBe(false);
	});
});
