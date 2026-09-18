import { rebasedDraftFromBlocks } from './rebase';
import type { MergeBlock, ThreeWaySegment } from './types';

/**
 * 行内备注编辑的草稿状态机（纯函数，便于单测）。
 *
 * mode:
 * - idle：未编辑，展示服务端当前备注与修订号；
 * - editing：场记正在编辑；
 * - saving：保存请求在途；
 * - conflict：与其他终端的改动重叠，三方片段就地展示，整理后可再次保存。
 *
 * 关键约定：网络失败与 409 冲突都不会清空 draft —— 场记整理服务端与本地
 * 文本后必须能用同一份输入再次保存。
 *
 * 冲突时 draft 不是“旧草稿原样保留 + 推进基础号”：那样会以当前修订号直存
 * 一份不含远端非冲突改动的旧全文，静默覆盖其他终端的不相交修改。相反，
 * 409 携带的服务端有序合并脚手架（mergeBlocks）会把 draft 真正重基到服务端
 * 全文——干净区域（含远端改动）逐字保留，仅冲突区域先填入本端文字待整理。
 */
export type NoteDraftMode = 'idle' | 'editing' | 'saving' | 'conflict';

export interface NoteDraftState {
  mode: NoteDraftMode;
  draft: string;
  /** 本次保存所基于的修订号；冲突解决后推进到服务端当前修订号。 */
  baseRevision: number;
  error: string | null;
  conflictSegments: ThreeWaySegment[];
  /** 冲突时服务端当前文本与修订号（便于“先填入服务端文本再整理”）。 */
  serverNotes: string | null;
  serverRevision: number | null;
  /** 冲突时服务端复算的有序合并脚手架；draft 据此重基，干净块不可被旧草稿还原。 */
  mergeBlocks: MergeBlock[] | null;
}

export interface NoteConflictPayload {
  conflicts: ThreeWaySegment[];
  current: { notes: string; notes_revision: number } | null;
  /** 服务端 409 中的有序合并脚手架；缺失时退化为以服务端当前全文为草稿。 */
  mergeBlocks?: MergeBlock[] | null;
}

export function idleDraft(): NoteDraftState {
  return {
    mode: 'idle',
    draft: '',
    baseRevision: 0,
    error: null,
    conflictSegments: [],
    serverNotes: null,
    serverRevision: null,
    mergeBlocks: null,
  };
}

export function startDraft(
  current: { notes: string; notes_revision: number },
): NoteDraftState {
  return {
    ...idleDraft(),
    mode: 'editing',
    draft: current.notes,
    baseRevision: current.notes_revision,
  };
}

export function changeDraft(state: NoteDraftState, text: string): NoteDraftState {
  if (state.mode === 'idle' || state.mode === 'saving') return state;
  return { ...state, draft: text };
}

export function beginSave(state: NoteDraftState): NoteDraftState {
  if (state.mode !== 'editing' && state.mode !== 'conflict') return state;
  return { ...state, mode: 'saving', error: null };
}

export function saveSucceeded(): NoteDraftState {
  // 看板行由 PATCH 响应刷新；编辑器回到只读展示。
  return idleDraft();
}

export function saveRetryable(state: NoteDraftState, message: string): NoteDraftState {
  if (state.mode !== 'saving') return state;
  // 网络失败/5xx：结果未知，保留输入与原基础修订号，回到可编辑状态直接重试。
  return {
    ...state,
    mode: state.conflictSegments.length > 0 ? 'conflict' : 'editing',
    error: message,
  };
}

export function saveRejected(state: NoteDraftState, message: string): NoteDraftState {
  if (state.mode !== 'saving') return state;
  // 其它 4xx（如备注超长）：请求未被接受，输入保留以便修改后重试。
  return { ...state, mode: 'editing', error: message };
}

export function saveNotesConflict(
  state: NoteDraftState,
  payload: NoteConflictPayload,
  message: string,
): NoteDraftState {
  if (state.mode !== 'saving') return state;
  const serverNotes = payload.current?.notes ?? null;
  const serverRevision = payload.current?.notes_revision ?? null;
  // 重叠冲突：把草稿真正重基到服务端当前全文——干净区域（包含其他终端的
  // 非冲突改动）逐字保留，仅冲突区域先填入本端文字等待整理。基础修订号随之
  // 推进到服务端当前版本。若服务端未给脚手架（旧版本混用），退化为直接以
  // 服务端当前全文为草稿，同样不会覆盖任何远端改动。
  return {
    ...state,
    mode: 'conflict',
    error: message,
    conflictSegments: payload.conflicts,
    serverNotes,
    serverRevision,
    mergeBlocks: payload.mergeBlocks ?? null,
    baseRevision: serverRevision ?? state.baseRevision,
    draft: rebasedDraftFromBlocks(payload.mergeBlocks, serverNotes ?? state.draft),
  };
}

export function useServerText(state: NoteDraftState): NoteDraftState {
  if (state.mode !== 'conflict' || state.serverNotes === null) return state;
  return { ...state, draft: state.serverNotes, error: null };
}
