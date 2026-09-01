const fs = require('fs');
const p = 'C:\\Users\\maimai\\Documents\\ChatGPT\\继续做agent\\app_patched2.py';
let c = fs.readFileSync(p, 'utf-8');
let n = 0;

// Patch 1: llm_extract_evidence
const ev1 = c.indexOf('猎头策略分析助手。下面是一份猎头访谈记录');
if (ev1 < 0) throw 'Patch1: not found';
const evSys = c.lastIndexOf('system_prompt = (', ev1);
const evUser = c.indexOf('user_prompt = "发言列表', ev1);
const evEnd = c.indexOf('\n', evUser) + 1;
const evOld = c.substring(evSys, evEnd);
const evNew = [
  'expert_name = source.get("target_expert", "")',
  '    system_prompt = (',
  '        "你是一个猎头策略分析助手。目标专家是：" + expert_name + "。下面是一份猎头访谈记录的发言列表。"',
  '        "请逐条分析每条发言，为每条发言给出以下分类之一：\\n"',
  '        "- strategy: 目标专家（" + expert_name + "）亲口描述了电话策略、动作、流程或判断标准\\n"',
  '        "- script: 目标专家（" + expert_name + "）亲口说了可直接用于电话的原话话术\\n"',
  '        "- context: 上下文、提问、引子，或非目标专家（非" + expert_name + "）的发言——这些仅作为理解目标专家发言的背景，不提炼为策略\\n"',
  '        "- meta: 会议元信息、寒暄或无关内容\\n"',
  '        "重要：只有目标专家（" + expert_name + "）亲口说的内容才能标为 strategy 或 script。"',
  '        "其他人（访谈者、同事等）的策略建议、观点或话术，即使被目标专家以对、嗯、是等方式肯定，也标为 context，"',
  '        "并在 reason 中注明目标专家对某某的肯定表态。不要把别人的策略当成目标专家的策略。"',
  '        "请以 JSON 数组返回，每项包含 utterance_id、evidence_kind、reason（一句话理由）。"',
  '        "只返回 JSON，不要其他文字。"',
  '    )',
  '',
  '    user_prompt = "发言列表：\\n" + transcript_text'
].join('\r\n');
c = c.replace(evOld, evNew);
n++; console.log('Patch 1 applied');

fs.writeFileSync(p, c, 'utf-8');
console.log('Saved, patches:', n);