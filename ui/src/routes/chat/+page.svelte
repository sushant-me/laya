<!-- Copyright 2026 Aayush Chawla -->
<!-- SPDX-License-Identifier: Apache-2.0 -->
<script lang="ts">
	import { get } from 'svelte/store';
	import { tick } from 'svelte';
	import { engineApi } from '$lib/api/engine';
	import {
		activeConversationId,
		activeTools,
		chatMessages,
		chatSending,
		conversations,
		streamingMessageId
	} from '$lib/stores/chat';
	import { applyLoadedMessages } from '$lib/stores/chatStream';
	import { sendChatMessage } from '$lib/chat/send';
	import { canSubmit, pendingLabel, resolveComposerAction } from '$lib/chat/composer';
	import ChatMessage from '$lib/components/chat/ChatMessage.svelte';
	import ChatConversationList from '$lib/components/chat/ChatConversationList.svelte';
	import { glassTheme } from '$lib/stores/glassTheme';

	// Assistant focus for every turn sent from this page. The engine resolves
	// this id against prompts.chat.CHAT_FOCUS_PROMPTS and ignores ids it does not
	// know, so this page opting in cannot change any other surface's behaviour —
	// the sidebar and Omni keep sending without a focus.
	//
	// The focus is per-REQUEST, not persisted on the conversation row: the
	// sidebar (rendered by the layout on every route) sends no focus, so a turn
	// sent from the sidebar into the same shared conversation is answered
	// without the coding persona. The "Coding" badge below therefore scopes its
	// claim to messages sent from THIS page rather than to the whole thread.
	const CODING_FOCUS = 'coding';

	// Mirrors ChatSidebar: follow new content only while pinned to the bottom, so
	// reading earlier content during a stream is not yanked back down.
	const SCROLL_PIN_THRESHOLD = 40;

	let input = $state('');
	let railOpen = $state(true);
	let pinnedToBottom = $state(true);
	let messagesEl: HTMLDivElement | undefined = $state();
	let textareaEl: HTMLTextAreaElement | undefined = $state();

	const busy = $derived($chatSending || $streamingMessageId !== null);
	const isEmpty = $derived($chatMessages.length === 0);
	const status = $derived(pendingLabel(busy, $activeTools));

	const conversationTitle = $derived.by(() => {
		const convId = $activeConversationId;
		if (!convId) return 'New chat';
		return $conversations.find((c) => c.conversation_id === convId)?.title ?? 'Chat';
	});

	// Suggestions are plain text dropped into the composer — never auto-sent —
	// so the first screen has something to click without the page inventing a
	// prompt for the user.
	const SUGGESTIONS = [
		'Summarize what is waiting on me right now',
		'Draft a fix plan for the most urgent open card',
		'Explain this stack trace and where to look first'
	];

	// One-shot hydration. The chat stores are shared with the sidebar, so
	// navigating here usually finds the conversation already in memory; only a
	// fresh page load (or a cold reopen) has the id set with an empty message
	// list. Guarded by a plain (non-reactive) flag because it must not re-run
	// when the stores change underneath it.
	let hydrated = false;
	$effect(() => {
		if (hydrated) return;
		hydrated = true;
		const convId = get(activeConversationId);
		if (!convId) return;
		// Header title comes from the shared conversations store, which the rail
		// fills on mount. If the rail has not opened yet the store is empty, so
		// fill it here rather than render "Chat" for a named conversation.
		if (get(conversations).length === 0) {
			engineApi
				.getConversations(100)
				.then((list) => conversations.set(list))
				.catch(() => {});
		}
		if (get(chatMessages).length === 0) {
			// applyLoadedMessages (not a plain set) so a reply still streaming into
			// this conversation is not clobbered by the lagging DB copy.
			engineApi
				.getConversationMessages(convId, 50)
				.then((msgs) => applyLoadedMessages(convId, [...msgs].reverse()))
				.catch(() => {});
		}
	});

	// Auto-grow the single-line input up to a cap, then scroll inside it.
	function resizeTextarea() {
		if (!textareaEl) return;
		textareaEl.style.height = 'auto';
		textareaEl.style.height = Math.min(textareaEl.scrollHeight, 200) + 'px';
	}

	$effect(() => {
		if (!textareaEl) return;
		input; // track
		resizeTextarea();
	});

	function scrollToBottom() {
		if (messagesEl) messagesEl.scrollTop = messagesEl.scrollHeight;
	}

	function handleMessagesScroll() {
		if (!messagesEl) return;
		const distanceFromBottom =
			messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight;
		pinnedToBottom = distanceFromBottom <= SCROLL_PIN_THRESHOLD;
	}

	$effect(() => {
		if ($chatMessages.length > 0 && pinnedToBottom) {
			// After the DOM update, so the scroll container exists on the first
			// message (the empty state has no messages element).
			setTimeout(scrollToBottom, 0);
		}
	});

	// Re-pin when switching conversations so a thread opens at its latest message.
	$effect(() => {
		$activeConversationId;
		pinnedToBottom = true;
	});

	// Focus the composer on arrival — this page is a chat, so the keyboard
	// belongs in the input.
	$effect(() => {
		tick().then(() => textareaEl?.focus());
	});

	// The input is disabled for the duration of a reply (that is how the busy
	// state is shown), and a disabled element loses focus — so re-focus when the
	// turn ends, or every answer would leave the composer without a keyboard.
	let wasBusy = false;
	$effect(() => {
		if (wasBusy && !busy) tick().then(() => textareaEl?.focus());
		wasBusy = busy;
	});

	function handleComposerKey(e: KeyboardEvent) {
		switch (resolveComposerAction({
			key: e.key,
			shiftKey: e.shiftKey,
			isComposing: e.isComposing,
			busy,
			hasText: input.trim().length > 0
		})) {
			case 'send':
				e.preventDefault();
				void send();
				break;
			case 'block':
				// Swallow Enter while busy/empty so it cannot insert a stray
				// newline — matches the sidebar, which always preventDefault()ed.
				e.preventDefault();
				break;
			default:
				break;
		}
	}

	async function send() {
		// Shared send path (optimistic bubble, WS-first with REST fallback, error
		// bubble) — the same module the sidebar uses, so the two surfaces cannot
		// drift. Returns false when the turn was refused (busy/empty).
		//
		// attachCardContext: false — the card stores are shared with the sidebar,
		// and this is a general coding conversation. Without this opt-out the card
		// the user asked the sidebar about would be sent as card_context on every
		// full-page turn AND persisted as this conversation's card_ids (see
		// $lib/chat/send.ts). This page never attaches card context, so there is
		// nothing here to disclose to the user.
		if (await sendChatMessage(input, { focus: CODING_FOCUS, attachCardContext: false })) {
			input = '';
			pinnedToBottom = true;
		}
	}

	function applySuggestion(text: string) {
		input = text;
		tick().then(() => textareaEl?.focus());
	}

	function startNewChat() {
		// Refuse while a reply is streaming: clearing the stores here would drop
		// the in-flight assistant message from the view (the stream keeps running
		// and lands in the DB, but the page would look like it lost the reply).
		if (busy) return;
		activeConversationId.set(null);
		chatMessages.set([]);
		input = '';
		tick().then(() => textareaEl?.focus());
	}
</script>

<div class="flex h-full min-h-0 gap-4">
	<!-- Conversation rail. Reuses the sidebar's list component (its window
	     controls are hidden — they act on the sidebar, which is not rendered
	     here). Hidden below sm, where there is no room for a rail; the toggle is
	     hidden with it so no dead control is shown — this is a desktop app and
	     the docked sidebar chat remains available at any width. -->
	{#if railOpen}
		<aside
			class="hidden w-72 shrink-0 overflow-hidden rounded-xl sm:flex sm:flex-col
				{$glassTheme ? 'glass-panel' : 'border border-surface-700 bg-surface-900'}"
		>
			<ChatConversationList showWindowControls={false} />
		</aside>
	{/if}

	<section class="flex min-h-0 min-w-0 flex-1 flex-col">
		<!-- Header -->
		<header
			class="flex h-11 shrink-0 items-center gap-2 rounded-xl px-3
				{$glassTheme ? 'glass-panel' : 'border border-surface-700 bg-surface-900'}"
		>
			<button
				onclick={() => (railOpen = !railOpen)}
				aria-label={railOpen ? 'Hide conversations' : 'Show conversations'}
				class="hidden shrink-0 rounded-md p-1 text-surface-400 transition-colors hover:text-surface-200 sm:block
					{$glassTheme ? 'glass-hover' : 'hover:bg-surface-800'}"
			>
				<svg class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" d="M4 6h16M4 12h16M4 18h16" />
				</svg>
			</button>
			<h1 class="min-w-0 flex-1 truncate text-laya-base font-semibold text-surface-200">
				{conversationTitle}
			</h1>
			<span
				class="shrink-0 rounded-full bg-laya-orange/10 px-2 py-0.5 text-laya-micro font-medium text-laya-orange ring-1 ring-laya-orange/30"
				title="Messages sent from this page use the coding focus. The focus is per-request, so a reply to a message sent from the sidebar in the same conversation does not."
			>Coding</span>
			<button
				onclick={startNewChat}
				disabled={busy}
				aria-label="New chat"
				title="New chat"
				class="shrink-0 rounded-md p-1 text-surface-400 transition-colors hover:text-laya-orange disabled:opacity-30
					{$glassTheme ? 'glass-hover' : 'hover:bg-surface-800'}"
			>
				<svg class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
					<path stroke-linecap="round" stroke-linejoin="round" d="M12 4v16m8-8H4" />
				</svg>
			</button>
		</header>

		<!-- Body: centered greeting with the composer in the middle when there is
		     no conversation yet (the first-screen layout); otherwise the message
		     column scrolls and the composer stays pinned to the bottom. The
		     composer element itself is rendered once, outside the branch, so
		     switching between the two layouts never remounts it and never drops
		     focus mid-typing.

		     Centering is done with auto margins (mt-auto on the greeting, mb-auto
		     on the composer — the two inner auto margins split the free space
		     around the PAIR), not justify-center: a centred flex column that
		     overflows clips its own top with no way to scroll to it, which is
		     exactly what would happen on a short window. With auto margins the
		     space collapses to zero when there is none and the container scrolls
		     instead. -->
		<div class="flex min-h-0 flex-1 flex-col {isEmpty ? 'overflow-y-auto' : ''}">
			{#if isEmpty}
				<div class="mt-auto px-4 text-center">
					<p class="text-laya-secondary font-medium tracking-wide text-laya-orange/80">
						Coding workspace
					</p>
					<h2 class="mx-auto mt-3 max-w-2xl text-balance text-3xl font-semibold leading-tight text-surface-100 sm:text-4xl">
						What are we building?
					</h2>
					<p class="mx-auto mt-4 max-w-xl text-laya-base leading-relaxed text-surface-400">
						Ask about a bug, a failing build, a PR, or paste an error. Laya answers as a
						coding assistant with your cards, events, and threads in reach.
					</p>
					<div class="mx-auto mt-8 flex max-w-2xl flex-wrap items-center justify-center gap-2">
						{#each SUGGESTIONS as suggestion (suggestion)}
							<button
								onclick={() => applySuggestion(suggestion)}
								class="rounded-full px-3.5 py-1.5 text-laya-secondary text-surface-400 transition-colors hover:text-laya-orange
									{$glassTheme ? 'glass-panel glass-hover' : 'border border-surface-700 bg-surface-900 hover:bg-surface-800'}"
							>{suggestion}</button>
						{/each}
					</div>
				</div>
			{:else}
				<div
					bind:this={messagesEl}
					onscroll={handleMessagesScroll}
					class="min-h-0 flex-1 overflow-y-auto"
				>
					<!-- max-w-3xl (48rem) centered column — the Gemini reading measure,
					     deliberately narrower than the page. -->
					<div class="mx-auto w-full max-w-3xl space-y-5 px-4 py-6">
						{#each $chatMessages as msg (msg.message_id)}
							<ChatMessage message={msg} streaming={msg.message_id === $streamingMessageId} />
						{/each}

						<!-- Tool-calling indicator (activeTools is fed by the shared stream) -->
						{#if $activeTools.length > 0}
							<div class="flex justify-start">
								<div
									class="rounded-xl px-3.5 py-2 text-laya-secondary text-surface-400
										{$glassTheme ? 'bg-white/[0.05] ring-1 ring-white/[0.08]' : 'bg-surface-800 ring-1 ring-surface-600'}"
								>
									<span class="mr-1.5 inline-block h-2 w-2 animate-pulse rounded-full bg-laya-orange"></span>
									Looking up: {$activeTools.join(', ')}
								</div>
							</div>
						{/if}

						<!-- Waiting indicator: sent, no stream event yet -->
						{#if busy && !$streamingMessageId && $activeTools.length === 0}
							<div class="flex justify-start">
								<div class="rounded-xl px-3.5 py-2.5 {$glassTheme ? 'bg-white/[0.06]' : 'bg-surface-700'}">
									<span class="inline-flex gap-1 text-surface-400">
										<span class="animate-bounce">.</span>
										<span class="animate-bounce" style="animation-delay: 0.1s">.</span>
										<span class="animate-bounce" style="animation-delay: 0.2s">.</span>
									</span>
								</div>
							</div>
						{/if}
					</div>
				</div>
			{/if}

			<!-- Composer. In the empty state it sits under the greeting, centered
			     (mb-auto completes the auto-margin pair above); otherwise it is the
			     bottom bar of the column (outside the scroll container, so it never
			     scrolls away — the "sticky" composer). -->
			<div class="w-full shrink-0 px-4 {isEmpty ? 'mb-auto mt-8 pb-2' : 'pb-4'}">
				<div class="mx-auto w-full max-w-3xl">
					<div
						class="rounded-2xl px-3 py-2
							{$glassTheme ? 'glass-panel' : 'border border-surface-700 bg-surface-900'}
							{status ? 'ring-1 ring-laya-orange/30' : ''}"
					>
						<textarea
							bind:this={textareaEl}
							bind:value={input}
							onkeydown={handleComposerKey}
							disabled={busy}
							rows={1}
							placeholder={busy ? 'Waiting for Laya…' : 'Ask about code, cards, or paste an error…'}
							aria-label="Message"
							class="w-full resize-none bg-transparent py-1.5 text-laya-base text-surface-200 placeholder-surface-500 focus:outline-none disabled:opacity-60"
							style="max-height: 200px; overflow-y: auto;"
						></textarea>
						<div class="mt-1 flex items-center justify-between gap-3">
							<!-- Visible busy state: the composer is disabled while a reply
							     streams, and a disabled input with no explanation reads as a
							     broken app. -->
							<span class="min-w-0 truncate text-laya-micro text-surface-500">
								{#if status}
									<span class="mr-1.5 inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-laya-orange align-middle"></span>
									{status}
								{:else}
									Enter to send · Shift+Enter for a new line
								{/if}
							</span>
							<button
								onclick={send}
								disabled={!canSubmit(input, busy)}
								aria-label="Send message"
								class="shrink-0 rounded-lg p-1.5 transition-colors disabled:opacity-30
									{canSubmit(input, busy) ? 'bg-laya-orange/15 text-laya-orange hover:bg-laya-orange/25' : 'text-surface-600'}"
							>
								<svg class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
									<path stroke-linecap="round" stroke-linejoin="round" d="M5 12h14M12 5l7 7-7 7" />
								</svg>
							</button>
						</div>
					</div>
				</div>
			</div>
		</div>
	</section>
</div>
