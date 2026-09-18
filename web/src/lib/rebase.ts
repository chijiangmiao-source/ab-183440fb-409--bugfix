import type { MergeBlock } from './types';

/**
 * 由服务端 409 返回的有序合并脚手架构建“真正重基”的冲突解决草稿。
 *
 * - text 块：无冲突区域的已合并文本——既包含两侧一致的内容，也包含远端独有的
 *   非冲突改动，原样保留；
 * - conflict 块：仍需场记整理的重叠区域，初始取本端文本（`mine`），保证输入不丢。
 *
 * 拼出的草稿在所有非冲突区域与服务端的合并结果逐字一致（远端不相交改动天然
 * 在内），因此以服务端当前修订号再次保存时，不可能像“旧草稿 + 新基础号”那样
 * 把远端不相交改动静默还原；场记只需对明确展示的冲突片段做出取舍。
 *
 * `fallback` 用于服务端未提供脚手架（如新旧版本混用）的场合：直接以服务端
 * 当前全文为草稿起点，同样不会覆盖任何远端改动（本端文字仍可在冲突面板中查看）。
 */
export function rebasedDraftFromBlocks(
  blocks: MergeBlock[] | null | undefined,
  fallback: string,
): string {
  if (!blocks || blocks.length === 0) return fallback;
  return blocks
    .map((block) => (block.type === 'text' ? block.text : block.mine))
    .join('');
}
