# 未起量迭代 — Excel解析提示词

以下是未起量迭代立项Excel表格的完整内容：

{sheets_content}

---

请从以上Excel内容中提取以下字段，返回JSON。字段按表格实际区域分组，请严格按语义理解识别，不要依赖固定列位置。

【一、基础信息】
- categoryl1: 一级品类
- categoryl2: 二级品类
- categoryl3: 三级品类
- project_name: 产品名称
- product_code: 自家产品型号/货号（如HY63、HW51，注意是自家产品不是竞品）
- brand: 产品品牌
- applicant: 负责人
- market_size: 市场大小（带时间维度，如"5000万/年"）
- estimated_sales: 目标销售额（带时间维度，如"300万/年"）
- pricing: 价格/毛利（保留原格式，如"139.9元/74%"）
- erp_cost: ERP成本（提取数字，如36.3）

【二、群体分析】
- people_analysis: 人群分析的完整原文（包括人群描述、需求、痛点等，原样保存）
- scene_analysis: 场景分析的完整原文（包括场景描述、场景排序等，原样保存）
- group_extra: 群体分析区域中除人群和场景外的其它内容（如核心人群分析、痛点总结等），找不到则设为null

【三、现状诊断】（未起量迭代的核心，请务必提取）
- failure_analysis: 没卖好的原因分析（完整原文，如"面料手感差导致差评多""定价偏高不在目标价格带""人群定位偏差"等。这是最重要的字段，请务必从表格中找到相关描述）
- current_issues: 当前产品存在的具体问题（完整原文，如"1.面料起球 2.版型偏大 3.包装简陋"等，描述产品本身的缺陷）
- sales_data_desc: 销量现状描述（如"上架3个月，月销50-100件""竞品月销2000+，我方月销不到100"等，找不到则设为null）

【四、产品设计要求】
- product_code: 自家产品货号（与基础信息中的product_code一致，若该区域有更具体的货号信息则以这里为准）
- design_purpose: 设计目的概述（保留原文，描述迭代方向和目的）
- upgrade_modules: 具体迭代模块（完整描述，如"1.面料升级为冰丝 2.版型调整为修身款 3.增加品牌logo"）
- upgrade_valiable: 迭代可行性分析（完整原文，描述模块获取方式、供应链可行性、开模难度等）

【五、竞品信息】
- competitor_name: 竞品名称/品牌（如"LP""迈克达威""南极人"，找不到则设为null）
- competitor_price: 竞品价格（保留原格式，如"139/只、129/只"）
- competitor_url: 竞品链接（URL，找不到则设为null）

== 注意事项 ==
1. 找不到的字段设为null，数组字段找不到则设为空数组[]
2. product_code是自家产品货号，务必提取
3. people_analysis和scene_analysis保留原文，不要拆分或重组
4. failure_analysis和current_issues是未起量迭代的核心字段，请务必从表格中找到相关描述。如果表格中有"问题分析""原因分析""销量分析"等区域，请完整提取
5. sales_data_desc描述销量现状，如果表格中有销量数据或对比数据请提取
6. 返回纯JSON，不要加```json```包裹
