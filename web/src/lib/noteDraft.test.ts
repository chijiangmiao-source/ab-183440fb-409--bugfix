import { describe, expect, it } from 'vitest';
import {
  beginSave,
  changeDraft,
  idleDraft,
  saveNotesConflict,
  saveRejected,
  saveRetryable,
  saveSucceeded,
  startDraft,
  useRebaseTemplate,
  useServerText,
} from './noteDraft';

const current = { notes: '原始备注\n第二行\n', notes_revision: 2 };

describe('noteDraft 状态机', () => {
  it('进入编辑时以服务端当前文本与修订号为草稿基础', () => {
    const state = startDraft(current);
    expect(state.mode).toBe('editing');
    expect(state.draft).toBe(current.notes);
    expect(state.baseRevision).toBe(2);
  });

  it('保存中：编辑/保存中/冲突 状态转换，控件锁定但输入保留', () => {
    let state = startDraft(current);
    state = changeDraft(state, '改后的备注');
    state = beginSave(state);
    expect(state.mode).toBe('saving');
    expect(state.draft).toBe('改后的备注');
    // 保存中不允许再改文本
    expect(changeDraft(state, '别的')).toBe(state);
  });

  it('网络失败后保留输入，回到可编辑状态并可用同一基础修订号重试', () => {
    let state = beginSave(changeDraft(startDraft(current), '我的改动'));
    state = saveRetryable(state, '网络异常');
    expect(state.mode).toBe('editing');
    expect(state.draft).toBe('我的改动');
    expect(state.baseRevision).toBe(2);
    expect(state.error).toContain('网络异常');
  });

  it('409 重叠冲突：载入重基模板、展示三方片段、基础修订号推进并记住编辑来源', () => {
    let state = beginSave(changeDraft(startDraft(current), '本地的改动'));
    state = saveNotesConflict(
      state,
      {
        conflicts: [{ base: '原始备注\n', mine: '本地的改动\n', theirs: '远端的改动\n' }],
        current: { notes: '远端的改动\n第二行\n', notes_revision: 3 },
        // 模板已含远端非冲突改动，冲突区保留本终端原文
        rebaseTemplate: '远端的改动\n第二行\n（远端新加的第三行）\n',
      },
      '与其他终端的修改冲突',
    );
    expect(state.mode).toBe('conflict');
    // 输入框载入重基模板，而不是保留可能缺远端改动的旧草稿
    expect(state.draft).toBe('远端的改动\n第二行\n（远端新加的第三行）\n');
    expect(state.baseRevision).toBe(3);
    expect(state.serverRevision).toBe(3);
    // 解决保存要回传的“编辑来源”：被拒那次保存基于 r2、原文是“本地的改动”
    expect(state.resolutionBaseRevision).toBe(2);
    expect(state.conflictNotes).toBe('本地的改动');
    expect(state.conflictSegments).toHaveLength(1);
    expect(state.conflictSegments[0].theirs).toBe('远端的改动\n');

    // 冲突后再遇网络失败：仍停留在冲突态，模板与来源信息继续保留
    state = beginSave(state);
    state = saveRetryable(state, 'HTTP 503');
    expect(state.mode).toBe('conflict');
    expect(state.resolutionBaseRevision).toBe(2);

    // “先填入服务端全文再整理”
    state = useServerText(state) as ReturnType<typeof useServerText>;
    expect(state.draft).toBe('远端的改动\n第二行\n');

    // 整理后保存成功：编辑器收起
    state = beginSave(state);
    expect(saveSucceeded().mode).toBe('idle');
  });

  it('报告复现：409 后旧草稿只推进基础号会缺远端非冲突改动，必须载入重基模板', () => {
    // r0：第一行/第二行/第三行；A 基于 r0 改第二行；B 先保存同时改第二、三行成为 r1。
    const r0 = { notes: '第一行\n第二行\n第三行', notes_revision: 0 };
    let state = beginSave(changeDraft(startDraft(r0), '第一行\nA第二行\n第三行'));

    // A 收到第二行冲突的 409，服务端 r1 为 B第二行 + B第三行。
    state = saveNotesConflict(
      state,
      {
        conflicts: [{ base: '第二行\n', mine: 'A第二行\n', theirs: 'B第二行\n' }],
        current: { notes: '第一行\nB第二行\nB第三行', notes_revision: 1 },
        // 正确的重基模板：保留 B 的第三行非冲突改动，冲突区放 A 的原文
        rebaseTemplate: '第一行\nA第二行\nB第三行',
      },
      '冲突',
    );

    // 关键断言：草稿不再是缺 B第三行的旧草稿，而是含 B第三行的模板
    expect(state.draft).toBe('第一行\nA第二行\nB第三行');
    expect(state.draft).not.toBe('第一行\nA第二行\n第三行');
    expect(state.baseRevision).toBe(1);
    expect(state.resolutionBaseRevision).toBe(0);
    expect(state.conflictNotes).toBe('第一行\nA第二行\n第三行');

    // 场记只整理冲突的第二行，第三行沿用模板，即可安全再次保存
    state = changeDraft(state, '第一行\nA+B第二行\nB第三行');
    state = beginSave(state);
    expect(saveSucceeded().mode).toBe('idle');
  });

  it('notes_unrebased 二次拒绝：保留场记刚输入的文本并更新模板，来源不被覆盖', () => {
    const r0 = { notes: '第一行\n第二行\n第三行', notes_revision: 0 };
    let state = beginSave(changeDraft(startDraft(r0), '第一行\nA第二行\n第三行'));
    state = saveNotesConflict(
      state,
      {
        conflicts: [{ base: '第二行\n', mine: 'A第二行\n', theirs: 'B第二行\n' }],
        current: { notes: '第一行\nB第二行\nB第三行', notes_revision: 1 },
        rebaseTemplate: '第一行\nA第二行\nB第三行',
      },
      '冲突',
    );

    // 场记误把第三行改回 r0 文本（丢失 B 的非冲突改动），服务端拒绝。
    state = changeDraft(state, '第一行\nA+B第二行\n第三行');
    state = beginSave(state);
    state = saveNotesConflict(
      state,
      {
        conflicts: [{ base: '第二行\n', mine: 'A第二行\n', theirs: 'B第二行\n' }],
        current: { notes: '第一行\nB第二行\nB第三行', notes_revision: 1 },
        // 服务端始终按最初触发冲突的原文重建模板（第二行回到 A 原文）
        rebaseTemplate: '第一行\nA第二行\nB第三行',
        unrebased: true,
      },
      '草稿未重基：缺少其他终端的非冲突改动',
    );
    expect(state.mode).toBe('conflict');
    // 场记刚输入的文本被保留，不被强制覆盖
    expect(state.draft).toBe('第一行\nA+B第二行\n第三行');
    // 错误可见，且可一键重新载入模板
    expect(state.error).toContain('未重基');
    state = useRebaseTemplate(state);
    expect(state.draft).toBe('第一行\nA第二行\nB第三行');
    // 编辑来源仍锚定最初那次 r0 编辑
    expect(state.resolutionBaseRevision).toBe(0);
    expect(state.conflictNotes).toBe('第一行\nA第二行\n第三行');
  });

  it('服务端未给模板时回退保留当前输入（旧服务端兼容）', () => {
    let state = beginSave(changeDraft(startDraft(current), '本地的改动'));
    state = saveNotesConflict(
      state,
      {
        conflicts: [],
        current: { notes: '服务端文本', notes_revision: 4 },
      },
      '冲突',
    );
    expect(state.draft).toBe('本地的改动');
    expect(state.baseRevision).toBe(4);
  });

  it('其它 4xx（如超长）保留输入以便精简后重试', () => {
    let state = beginSave(changeDraft(startDraft(current), '很长'.repeat(3000)));
    state = saveRejected(state, '备注超过 4000 字上限');
    expect(state.mode).toBe('editing');
    expect(state.draft.length).toBeGreaterThan(4000);
  });

  it('取消编辑放弃草稿并回到只读态', () => {
    let state = changeDraft(startDraft(current), '临时改动');
    expect(state.draft).toBe('临时改动');
    state = idleDraft();
    expect(state.mode).toBe('idle');
    expect(state.error).toBeNull();
  });
});
