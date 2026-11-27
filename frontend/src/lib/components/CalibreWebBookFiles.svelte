<script lang="ts">
	import { onDestroy } from 'svelte';
	import { api } from '#lib/api.js';
	import { _ } from '#lib/i18n/index.js';
	import type {
		Book,
		CalibreWebBookCandidate,
		CalibreWebBookFiles,
		CalibreWebUpload
	} from '#lib/types.js';

	let { book }: { book: Book } = $props();

	let files = $state<CalibreWebBookFiles>({
		configured: false,
		linked: false,
		calibre_book_id: null,
		library_id: null,
		metadata_synced_at: null,
		uploads: []
	});
	let loading = $state(false);
	let uploading = $state(false);
	let searching = $state(false);
	let matching = $state(false);
	let syncingMetadata = $state(false);
	let selectedFile = $state<File | null>(null);
	let replaceExisting = $state(false);
	let fileInput: HTMLInputElement | undefined = $state();
	let searchQuery = $state(book.title);
	let candidates = $state<CalibreWebBookCandidate[]>([]);
	let selectedCandidateId = $state('');
	let matchingUploadId = $state<number | null>(null);
	let message = $state<{ type: 'success' | 'error'; text: string } | null>(null);
	let pollTimer: ReturnType<typeof setInterval> | null = null;
	let hasActiveUpload = $derived(files.uploads.some((upload) => upload.status === 'queued' || upload.status === 'processing'));

	function stopPolling() {
		if (pollTimer !== null) {
			clearInterval(pollTimer);
			pollTimer = null;
		}
	}

	onDestroy(stopPolling);

	$effect(() => {
		const bookId = book.id;
		searchQuery = book.title;
		void load(bookId);
	});

	async function load(bookId: number) {
		loading = true;
		try {
			files = await api.calibreWeb.bookFiles(bookId);
			const pending = files.uploads.find((upload) => upload.status === 'needs_match');
			if (pending) {
				matchingUploadId = pending.id;
				void searchCandidates();
			} else {
				const active = files.uploads.find((upload) => upload.status === 'queued' || upload.status === 'processing');
				if (active) startPolling(active.id);
				else stopPolling();
			}
		} catch (error) {
			message = { type: 'error', text: error instanceof Error ? error.message : $_('profile.calibreWeb.notConnected') };
		} finally {
			loading = false;
		}
	}

	function startPolling(uploadId: number) {
		stopPolling();
		pollTimer = setInterval(() => {
			void refreshUpload(uploadId);
		}, 2500);
	}

	async function refreshUpload(uploadId: number) {
		try {
			const updated = await api.calibreWeb.uploadStatus(uploadId);
			files = {
				...files,
				uploads: files.uploads.map((upload) => upload.id === updated.id ? updated : upload)
			};
			if (updated.status === 'needs_match') {
				stopPolling();
				matchingUploadId = updated.id;
				void searchCandidates();
			} else if (updated.status === 'completed' || updated.status === 'failed') {
				stopPolling();
			}
		} catch (error) {
			stopPolling();
			message = { type: 'error', text: error instanceof Error ? error.message : $_('profile.calibreWeb.uploadFailed') };
		}
	}

	function handleFileChange(event: Event) {
		const target = event.currentTarget as HTMLInputElement;
		selectedFile = target.files?.[0] ?? null;
		message = null;
	}

	async function uploadEbook() {
		if (!selectedFile) return;
		uploading = true;
		message = null;
		try {
			const upload = await api.calibreWeb.uploadBook(book.id, selectedFile, replaceExisting);
			files = { ...files, uploads: [upload, ...files.uploads] };
			selectedFile = null;
			replaceExisting = false;
			if (fileInput) fileInput.value = '';
			if (upload.status === 'needs_match') {
				matchingUploadId = upload.id;
				void searchCandidates();
			} else if (upload.status === 'queued' || upload.status === 'processing') {
				startPolling(upload.id);
			}
		} catch (error) {
			message = { type: 'error', text: error instanceof Error ? error.message : $_('profile.calibreWeb.uploadFailed') };
		} finally {
			uploading = false;
		}
	}

	async function searchCandidates() {
		if (searchQuery.trim().length < 2) return;
		searching = true;
		try {
			candidates = [];
			const result = await api.calibreWeb.searchBooks(searchQuery.trim());
			candidates = result.items;
			selectedCandidateId = candidates.length === 1 ? String(candidates[0].id) : '';
		} catch (error) {
			message = { type: 'error', text: error instanceof Error ? error.message : $_('profile.calibreWeb.noCandidates') };
		} finally {
			searching = false;
		}
	}

	function candidateLabel(candidate: CalibreWebBookCandidate): string {
		const authors = Array.isArray(candidate.authors)
			? candidate.authors.join(', ')
			: typeof candidate.authors === 'string' ? candidate.authors : '';
		return `${candidate.title}${authors ? ` — ${authors}` : ''} (#${candidate.id})`;
	}

	async function linkSelectedBook() {
		if (matchingUploadId === null || !selectedCandidateId) return;
		matching = true;
		message = null;
		try {
			const result = await api.calibreWeb.matchUpload(
				matchingUploadId,
				Number(selectedCandidateId),
				files.library_id ?? 'default'
			);
			files = {
				...files,
				linked: true,
				calibre_book_id: result.calibre_book_id,
				uploads: files.uploads.map((upload) => upload.id === result.id ? result : upload)
			};
			matchingUploadId = null;
			candidates = [];
			if (result.metadata_sync_error) {
				message = { type: 'error', text: result.metadata_sync_error };
			} else {
				message = { type: 'success', text: $_('profile.calibreWeb.metadataSyncSuccess') };
			}
			await load(book.id);
		} catch (error) {
			message = { type: 'error', text: error instanceof Error ? error.message : $_('profile.calibreWeb.uploadFailed') };
		} finally {
			matching = false;
		}
	}

	async function syncMetadata() {
		syncingMetadata = true;
		message = null;
		try {
			const result = await api.calibreWeb.syncMetadata(book.id);
			if (result.synced) {
				message = { type: 'success', text: $_('profile.calibreWeb.metadataSyncSuccess') };
				await load(book.id);
			} else {
				message = { type: 'error', text: result.message ?? $_('profile.calibreWeb.metadataSyncFailed') };
			}
		} catch (error) {
			message = { type: 'error', text: error instanceof Error ? error.message : $_('profile.calibreWeb.metadataSyncFailed') };
		} finally {
			syncingMetadata = false;
		}
	}

	function statusLabel(status: string): string {
		const labels: Record<string, string> = {
			queued: 'profile.calibreWeb.statusQueued',
			processing: 'profile.calibreWeb.statusProcessing',
			needs_match: 'profile.calibreWeb.statusNeedsMatch',
			completed: 'profile.calibreWeb.statusCompleted',
			failed: 'profile.calibreWeb.statusFailed'
		};
		return labels[status] ? $_(labels[status]) : status;
	}
</script>

<section class="rounded-xl border border-base-200 p-3 flex flex-col gap-3">
	<div class="flex items-center justify-between gap-2">
		<h3 class="font-semibold text-sm">{$_('profile.calibreWeb.ebookFiles')}</h3>
		{#if files.linked}
			<span class="badge badge-success badge-sm">{$_('profile.calibreWeb.statusCompleted')}</span>
		{/if}
	</div>
	{#if message}
		<div class="alert {message.type === 'success' ? 'alert-success' : 'alert-error'} py-2 text-sm">
			<span>{message.text}</span>
			<button type="button" class="btn btn-ghost btn-xs" onclick={() => (message = null)}>{$_('common.close')}</button>
		</div>
	{/if}
	{#if loading}
		<p class="text-xs text-base-content/60">{$_('common.loading')}</p>
	{:else if !files.configured}
		<p class="text-xs text-base-content/70">{$_('profile.calibreWeb.connectFirst')}</p>
	{:else}
		<p class="text-xs text-base-content/60">{$_('profile.calibreWeb.fileHelp')}</p>
		{#if files.linked}
			<label class="label cursor-pointer justify-start gap-2 py-0">
				<input class="checkbox checkbox-sm" type="checkbox" bind:checked={replaceExisting} disabled={uploading || hasActiveUpload} />
				<span class="label-text text-xs">{$_('profile.calibreWeb.replaceExisting')}</span>
			</label>
		{/if}
		<div class="flex flex-col sm:flex-row gap-2">
			<input
				class="file-input file-input-bordered file-input-sm w-full"
				type="file"
				accept=".epub,.pdf,application/epub+zip,application/pdf"
				bind:this={fileInput}
				onchange={handleFileChange}
				disabled={uploading}
			/>
			<button type="button" class="btn btn-primary btn-sm shrink-0" onclick={uploadEbook} disabled={!selectedFile || uploading || hasActiveUpload}>
				{uploading ? $_('common.saving') : $_('profile.calibreWeb.upload')}
			</button>
		</div>
		{#if files.linked && files.calibre_book_id !== null}
			<div class="flex flex-wrap items-center justify-between gap-2 rounded-lg bg-base-200/50 p-2 text-xs">
				<span>{$_('profile.calibreWeb.linkedBook')}: <strong>{files.calibre_book_id}</strong></span>
				<button type="button" class="btn btn-outline btn-xs" onclick={syncMetadata} disabled={syncingMetadata}>
					{syncingMetadata ? $_('common.saving') : $_('profile.calibreWeb.syncMetadata')}
				</button>
			</div>
		{/if}
		{#if matchingUploadId !== null}
			<div class="rounded-lg border border-warning/30 bg-warning/5 p-3 flex flex-col gap-2">
				<p class="text-sm font-medium">{$_('profile.calibreWeb.statusNeedsMatch')}</p>
				<div class="flex flex-col sm:flex-row gap-2">
					<input
							class="input input-bordered input-sm flex-1"
							bind:value={searchQuery}
							placeholder={$_('profile.calibreWeb.searchTitle')}
							onkeydown={(event) => {
								if (event.key === 'Enter') {
									event.preventDefault();
									void searchCandidates();
								}
							}}
						/>
					<button type="button" class="btn btn-outline btn-sm" onclick={searchCandidates} disabled={searching || searchQuery.trim().length < 2}>
						{searching ? $_('common.loading') : $_('profile.calibreWeb.searchBooks')}
					</button>
				</div>
				{#if candidates.length > 0}
					<select class="select select-bordered select-sm w-full" bind:value={selectedCandidateId}>
						<option value="">—</option>
						{#each candidates as candidate (candidate.id)}
							<option value={String(candidate.id)}>{candidateLabel(candidate)}</option>
						{/each}
					</select>
					<button type="button" class="btn btn-primary btn-sm self-start" onclick={linkSelectedBook} disabled={!selectedCandidateId || matching}>
						{matching ? $_('common.saving') : $_('profile.calibreWeb.matchBook')}
					</button>
				{:else if !searching}
					<p class="text-xs text-base-content/60">{$_('profile.calibreWeb.noCandidates')}</p>
				{/if}
			</div>
		{/if}
		{#if files.uploads.length > 0}
			<div class="flex flex-col gap-2">
				<p class="text-xs font-semibold">{$_('profile.calibreWeb.uploadHistory')}</p>
				{#each files.uploads as upload (upload.id)}
					<div class="flex items-center justify-between gap-2 text-xs border-b border-base-200 pb-2">
						<span class="min-w-0 truncate">{upload.filename}</span>
						<span class="badge badge-ghost badge-sm shrink-0">{statusLabel(upload.status)}</span>
					</div>
					{#if upload.error_message}
						<p class="text-xs text-error break-words">{upload.error_message}</p>
					{/if}
				{/each}
			</div>
		{/if}
	{/if}
</section>
