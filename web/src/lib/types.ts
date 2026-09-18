export interface IssuedOperation {
  scene_id: string;
  client_op_id: string;
  /** 当前（可修订）备注 */
  notes: string;
  shot_number: number;
  created_at: string;
  /** 备注修订号：发放备注固定为 0，每次修订递增 */
  notes_revision: number;
}

export interface IssueResponse extends IssuedOperation {
  /** true 表示这是一次幂等重放，号码是此前已提交的原始号码 */
  replayed: boolean;
}

export interface IssueRequestBody {
  scene_id: string;
  client_op_id: string;
  notes: string;
  inject_failure_after_commit?: boolean;
}

export interface NoteUpdateRequestBody {
  client_op_id: string;
  base_revision: number;
  notes: string;
}

/** 备注修订成功响应；merge_status 为 "updated" 或 "merged"（落后时自动合并）。 */
export interface NoteUpdateResponse extends IssuedOperation {
  merge_status: 'updated' | 'merged';
}

/** 409 重叠冲突中的一个三方片段。 */
export interface ThreeWaySegment {
  base: string;
  mine: string;
  theirs: string;
}

/**
 * 409 响应里携带的三方合并有序脚手架：
 * - text：两侧一致（或服务端独有、已自动合并好）的干净文本，重基时原样保留；
 * - conflict：仍需场记整理的冲突区域（与 conflicts 中的三方片段一致）。
 *
 * 重基草稿 = 依次拼接每个 text 块与每个 conflict 块的“当前解决结果”。
 * 这样冲突解决草稿真正建立在服务端全文之上，远端非冲突改动不会被旧草稿还原。
 */
export type MergeBlock =
  | { type: 'text'; text: string }
  | ({ type: 'conflict' } & ThreeWaySegment);

export interface NoteRevision {
  revision: number;
  notes: string;
  created_at: string;
}
