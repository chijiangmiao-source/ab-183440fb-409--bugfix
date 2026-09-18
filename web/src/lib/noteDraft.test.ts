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

  it('409 重叠冲突：草稿重基到服务端全文、展示三方片段、基础修订号推进', () => {
    let state = beginSave(changeDraft(startDraft(current), '本地的改动\n第二行\n'));
    state = saveNotesConflict(
      state,
      {
        conflicts: [{ base: '原始备注\n', mine: '本地的改动\n', theirs: '远端的改动\n' }],
        current: { notes: '远端的改动\n第二行\n', notes_revision: 3 },
        // 服务端脚手架：第一行冲突，第二行为两侧一致的干净文本。
        mergeBlocks: [
          { type: 'conflict', base: '原始备注\n', mine: '本地的改动\n', theirs: '远端的改动\n' },
          { type: 'text', text: '第二行\n' },
        ],
      },
      '与其他终端的修改冲突',
    );
    expect(state.mode).toBe('conflict');
    // 冲突区域保留本端输入，干净区域（第二行）逐字保留：草稿真正重基到 r3 全文。
    expect(state.draft).toBe('本地的改动\n第二行\n');
    expect(state.baseRevision).toBe(3);
    expect(state.serverRevision).toBe(3);
    expect(state.mergeBlocks).toHaveLength(2);
    expect(state.conflictSegments).toHaveLength(1);
    expect(state.conflictSegments[0].theirs).toBe('远端的改动\n');

    // 冲突后再遇网络失败：仍停留在冲突态，重基后的输入继续保留
    state = beginSave(state);
    state = saveRetryable(state, 'HTTP 503');
    expect(state.mode).toBe('conflict');
    expect(state.draft).toBe('本地的改动\n第二行\n');

    // “先填入服务端文本再整理”
    state = useServerText(state) as ReturnType<typeof useServerText>;
    expect(state.draft).toBe('远端的改动\n第二行\n');

    // 整理后保存成功：编辑器收起
    state = beginSave(state);
    expect(saveSucceeded().mode).toBe('idle');
  });

  it('复现：B 同次保存的非冲突改动不会被 A 的旧草稿在二次保存时还原', () => {
    // r0 三行；A 从 r0 改第二行；B 在一次保存里同时改第二、三行成为 r1。
    const r0 = { notes: '第一行\n第二行\n第三行', notes_revision: 0 };
    let state = beginSave(changeDraft(startDraft(r0), '第一行\nA第二行\n第三行'));
    state = saveNotesConflict(
      state,
      {
        conflicts: [{ base: '第二行\n', mine: 'A第二行\n', theirs: 'B第二行\n' }],
        current: { notes: '第一行\nB第二行\nB第三行', notes_revision: 1 },
        // 关键：第三行作为 B 的非冲突改动落在干净文本块里。
        mergeBlocks: [
          { type: 'text', text: '第一行\n' },
          { type: 'conflict', base: '第二行\n', mine: 'A第二行\n', theirs: 'B第二行\n' },
          { type: 'text', text: 'B第三行' },
        ],
      },
      '第二行冲突',
    );

    // 基础号推进到 r1，且草稿已含 B 的第三行（不是 r0 的“第三行”）。
    expect(state.baseRevision).toBe(1);
    expect(state.draft).toBe('第一行\nA第二行\nB第三行');

    // 场记只在冲突片段内把第二行整理为 A+B，第三行原样保留；二次保存发出的
    // 全文（baseRevision 已为 r1）保留了远端非冲突改动。
    state = changeDraft(state, '第一行\nA+B第二行\nB第三行');
    expect(state.baseRevision).toBe(1);
    expect(state.draft).toBe('第一行\nA+B第二行\nB第三行');
  });

  it('409 未携带脚手架（旧服务端）时退化为服务端当前全文，绝不沿用旧草稿', () => {
    const r0 = { notes: '第一行\n第二行\n第三行', notes_revision: 0 };
    let state = beginSave(changeDraft(startDraft(r0), '第一行\nA第二行\n第三行'));
    state = saveNotesConflict(
      state,
      {
        conflicts: [{ base: '第二行\n', mine: 'A第二行\n', theirs: 'B第二行\n' }],
        current: { notes: '第一行\nB第二行\nB第三行', notes_revision: 1 },
      },
      '第二行冲突',
    );
    // 没有脚手架：直接以服务端当前全文为草稿起点，本端文字仍可在冲突面板中查看。
    expect(state.draft).toBe('第一行\nB第二行\nB第三行');
    expect(state.baseRevision).toBe(1);
    expect(state.mergeBlocks).toBeNull();
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
