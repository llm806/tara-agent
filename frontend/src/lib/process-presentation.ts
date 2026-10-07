import type { AgentStep } from "@/lib/types";

const analyses: Record<string, string> = {
  function_study: "功能排名、粒径和海区分布，以及功能信号与环境的关系",
  compare_function_signals: "DNA 与 RNA 中相同功能的相对贡献",
  function_environment: "目标功能信号与环境条件的关系",
  community_analysis: "群落分布、多样性及其与环境的关系",
  function_atlas: "各样本中的功能排名与相对贡献",
  function_profile: "指定样本中的功能组成",
  find_function_samples: "可用于功能分析的样本",
  retrieve_gene_sequences: "目标基因的核酸序列",
  taxon_abundance: "目标类群的丰度与组成",
  diversity_analysis: "样本中的生物多样性",
  environment_association: "环境条件与类群丰度的关系",
  find_samples: "符合条件的样本",
  get_sample_info: "样本的采集和环境信息",
  find_taxa: "目标类群的分类与出现记录",
};

// 旧会话也可能保存内部规划说明；只转换展示，完整原文仍在执行记录中。
export function processPresentation(step: AgentStep): AgentStep {
  const title = step.title
    .replace("识别请求类型", "理解你的问题")
    .replace("选择分析工作流", "安排分析步骤")
    .replace(/规划第 (\d+) 轮分析/, "确定第 $1 步分析")
    .replace("理解问题并选择工具", "确定分析方法")
    .replace("组织可追踪答案", "整理分析结果");
  const internal = /论文|任务[一二三四五六七八九十0-9]|工作流|契约|白名单|MCP|current_data|paper_reference|Top100|PF\d{5}|[a-z]+_[a-z_]+/i;
  if (!internal.test(step.detail)) return { ...step, title };
  const topics = Object.entries(analyses)
    .filter(([name]) => step.detail.includes(name)).map(([name, description]) => `${description}（${name}）`);
  const fallback: Record<AgentStep["stage"], string> = {
    route: "将使用当前可用数据分析你的问题。",
    understand: "根据你的问题安排查询、计算和比较步骤。",
    execute: "已返回分析数据，接下来检查结果是否满足你的要求。",
    answer: "根据已计算的结果整理结论、图表和需要说明的限制。",
    respond: "根据已有信息回答你的问题。",
  };
  return { ...step, title, detail: topics.length
    ? `${step.stage === "execute" ? "已返回以下分析的数据" : "将分析"}：${topics.join("；")}。`
    : fallback[step.stage] };
}
