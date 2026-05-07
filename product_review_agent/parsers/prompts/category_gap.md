# 品类缺失 — Excel解析提示词

以下是品类缺失立项Excel表格的完整内容：

{sheets_content}

---

请从以上Excel内容中提取以下字段，返回JSON。字段按表格实际区域分组，请严格按语义理解识别，不要依赖固定列位置。

【一、基础信息】
- categoryl1: 一级品类
- categoryl2: 二级品类
- categoryl3: 三级品类
- project_name: 产品名称
- brand: 产品品牌
- applicant: 负责人
- market_size: 市场大小（带时间维度，如"5000万/年"）
- estimated_sales: 目标销售额（带时间维度，如"300万/年"）
- pricing: 价格/毛利（保留原格式，如"139.9元/74%"）
- erp_cost: ERP成本（提取数字，如36.3）
- base_other: 基础信息区域中除以上字段外的其它有价值内容，找不到则设为null

【二、群体分析】
- people_analysis: 人群分析的完整原文（包括人群描述、需求、痛点等，原样保存）
- scene_analysis: 场景分析的完整原文（包括场景描述、场景排序等，原样保存）
- group_other: 群体分析区域中除人群和场景外的其它内容（如核心人群分析、痛点总结等），找不到则设为null

【三、竞品产品分析】
- competitor_url: 竞品商品链接（URL，找不到则设为null）
- competitor_price: 竞品价格（提取数字，如"89.9"，找不到则设为null）
- selling_point: 可复制的竞品卖点（完整原文，我方要学习/复制的）
- improving_point: 可超越的竞品卖点/自家卖点（完整原文，我方要超越/差异化的）
- competitor_other: 竞品产品分析区域中除以上字段外的其它有价值内容，找不到则设为null

【四、产品设计要求】
- design_purpose: 设计目的概述（ABC三大分类选择：A类=品牌爆款/核心利润款，B类=流量款/跑量款，C类=长尾款/补充款。提取字母即可，如"A"或"A类"）
- outlook: 改外观/品牌的具体描述
- material: 改材料的具体描述
- function: 改功能的具体描述
- design_other: 产品设计要求区域中除以上字段外的其它有价值内容，找不到则设为null

== 注意事项 ==
1. 找不到的字段设为null，数组字段找不到则设为空数组[]
2. competitor_price是竞品价格，不是自家价格，注意区分
3. people_analysis和scene_analysis保留原文，不要拆分或重组
4. design_purpose只需提取ABC分类字母，不需要描述
5. selling_point是竞品值得我方复制的卖点，improving_point是我方要超越/差异化的点，两者方向不同
6. 每个分组末尾的other字段用于兜底该区域中不属于已定义字段但有价值的内容，不要遗漏
7. 返回纯JSON，不要加```json```包裹
