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
  /**
   * 冲突解决保存时携带：触发 409 的那次编辑所基于的修订号与其原文。
   * 服务端据此校验本次草稿确实重基到了服务端全文（包含远端非冲突改动），
   * 而不是只把旧草稿的基础修订号改成最新版本号。
   */
  resolution_base_revision?: number;
  conflict_notes?: string;
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

export interface NoteRevision {
  revision: number;
  notes: string;
  created_at: string;
}
