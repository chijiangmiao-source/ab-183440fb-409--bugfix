import type { ThreeWaySegment } from './types';

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
 * 防覆盖约定：重叠冲突后，本地旧草稿其实基于更早的修订号；只把基础修订号
 * 推进到服务端当前版本、却原样提交旧草稿，会把其他终端的**非冲突**改动静默
 * 还原。因此冲突时：
 * - draft 直接换成服务端返回的“已重基模板”（含远端非冲突改动，冲突区保留
 *   本终端原文，不丢任何输入）；
 * - 记住触发冲突那次编辑的来源修订号（resolutionBaseRevision）与原文
 *   （conflictNotes），再次保存时一并提交，服务端据此校验草稿确实重基；
 * - 若服务端判定草稿仍未重基（notes_unrebased），会再次拒绝并返回新模板，
 *   状态停留在 conflict，数据库始终不动。
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
  /** 触发 409 的那次编辑实际基于的修订号（冲突解决保存时回传服务端校验）。 */
  resolutionBaseRevision: number | null;
  /** 触发 409 的那次编辑的原文（冲突解决保存时回传服务端校验）。 */
  conflictNotes: string | null;
  /** 服务端最新给出的已重基模板（可一键填入重新整理）。 */
  rebaseTemplate: string | null;
}

export interface NoteConflictPayload {
  conflicts: ThreeWaySegment[];
  current: { notes: string; notes_revision: number } | null;
  /** 服务端给出的已重基模板；缺省时回退为保留当前输入。 */
  rebaseTemplate?: string | null;
  /** true：草稿已推进过基础号但仍缺远端非冲突改动（notes_unrebased）。 */
  unrebased?: boolean;
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
    resolutionBaseRevision: null,
    conflictNotes: null,
    rebaseTemplate: null,
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
  // 重叠冲突：记录被拒编辑的真实来源（基础修订号 + 原文），供解决保存时
  // 证明草稿已重基；基础修订号推进到服务端当前版本。
  const serverRevision = payload.current?.notes_revision ?? state.baseRevision;
  return {
    ...state,
    mode: 'conflict',
    error: message,
    conflictSegments: payload.conflicts,
    serverNotes: payload.current?.notes ?? null,
    serverRevision,
    baseRevision: serverRevision,
    // 首次冲突：输入框换成服务端给的已重基模板（含远端非冲突改动，冲突区
    // 保留本终端原文，不丢输入）。notes_unrebased 二次拒绝：保留场记刚整理
    // 的文本，由其对照错误提示与模板按钮自行修正，不强行覆盖。
    draft:
      payload.unrebased || payload.rebaseTemplate == null
        ? state.draft
        : payload.rebaseTemplate,
    // 来源始终锚定在最初触发冲突的那次编辑，后续二次拒绝不再覆盖。
    resolutionBaseRevision: state.resolutionBaseRevision ?? state.baseRevision,
    conflictNotes: state.conflictNotes ?? state.draft,
    rebaseTemplate:
      payload.rebaseTemplate != null ? payload.rebaseTemplate : state.rebaseTemplate,
  };
}

export function useServerText(state: NoteDraftState): NoteDraftState {
  if (state.mode !== 'conflict' || state.serverNotes === null) return state;
  return { ...state, draft: state.serverNotes, error: null };
}

export function useRebaseTemplate(state: NoteDraftState): NoteDraftState {
  if (state.mode !== 'conflict' || state.rebaseTemplate === null) return state;
  return { ...state, draft: state.rebaseTemplate, error: null };
}
