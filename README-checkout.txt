最新状态（2026-11-16）：用户要求切换独立浏览器脚本。
  当前配置 browser_executor=direct，由 direct_browser_worker.py 直接启动 direct_checkout.mjs；无聊天/queue。
  QQ SMTP直发及0.1秒检测保持待命。专用Chrome与内置浏览器会话独立。
  真实黑色XS测试尚待登录成功；direct-browser-ready.json=false，正式结账暂未启用。
  direct_checkout.mjs只调用prepareCheckout(stopAt=payment)，不调用Review或提交入口。
  下文IAB说明保留作历史记录，以此最新状态及运行状态文件为准。

lululemon 澳洲 — Classic / Lavender Rock / US XS

当前脚本监控与事件处理的实际授权（2026-11-16更新）
  北京时间11月17日00:50–06:00监控；全窗口按0.1秒尝试发起，最多10轮并发。
  唯一精确Classic / Lavender Rock / US XS确认有货后发邮件，并复用内置浏览器准备到Payment。
  当前执行入口仅preparePaymentOnce；硬停止于填卡页，不选保存卡、不继续Review、不提交。
  该模式重新核对官方库存及触发SKU、PDP实际选择和购物袋，只加购目标1件。
  原Oversized / US S已按用户授权移到Saved Items。收藏区被排除在购物袋核对范围之外。
  官网检测与事件监听均由本地Python进程运行；只有真实目标事件才唤醒当前聊天，不再周期唤醒。
  通知见README-notifications.txt；以下live/Review模式是旧执行器能力，不是当前事件处理授权。

当前结构（2026-11-16）
  lululemon_monitor.py      只读官方商品/库存 HTTP GET，保留原始响应。
  lululemon_checkout.py     监控并交接候选，默认 browser=iab，不再另开 Chrome。
  iab_checkout.mjs          内置浏览器元素自动化：加购、地址、卡摘要、Review、一次提交、3DS 停止。
  iab_runner.mjs            连接官方接口检查与现有 IAB Tab，支持有界监控 tick。
  checkout_ui.py            旧 Chrome 测试版，仍禁止 Place order，需显式 --browser chrome。

运行边界
  新执行器需要 Codex 浏览器工具提供的现有内置浏览器 Tab。
  普通终端 node/Python 不能直接接管内置浏览器；没有复制 Cookie、登录状态或发现隐藏调试端口。
  单独运行 Python --watch 只监控并交接候选，不执行内置浏览器。
  旧monitorTick每次执行一个有界检查；当前使用availability_notifier.py和event_dispatcher.py本地进程。

固定目标
  Product ID: lopmi7nqah
  Scuba Evolve Classic Full-Zip Hoodie
  Lavender Rock / 77631 / LAVR，三个字段须一致
  US XS (AU 6-8)，1 件，Mastercard 尾号 0000
  拒绝 Oversized、Black、US S、组合色、Final Sale、未知币种。
  库存证据最多 120 秒，PDP 核对选中 SKU 后才加购，提交前再次查库存。

执行流程与默认值
  dryRun=true：只准备及核对 Review，不勾条款、不提交。
  live 需要精确授权对象：productId、sku、maxTotal、cardLast4、acceptSalesTerms=true、submitOnce=true。
  maxTotal 是含运费总价上限，必须为正数并与授权一致。
  本次 Oversized / US S / A$169 测试授权不能用于下一笔 Classic 订单。

  1. 复用内置浏览器并确认登录。原购物袋含其他商品则停止，不删除或代买。
  2. 用接口已验证的ID/SKU打开Classic，选择并核对实际颜色/尺码/SKU，只加购一次。
  3. 沿用事先核对且已选中的收货地址，不重排地址、不改 suburb；无需签名留件保持 No。
  4. 只选匹配的已保存 Mastercard 0000。缺卡、需安全码或付款步骤异常就停下，请本人处理。
     不读取/填写/记录完整卡号、有效期、CVV，不自动勾选保存卡。
  5. Review 核对商品、颜色、尺码、数量、AUD 总价和唯一卡尾号。
  6. 明确授权后勾条款，先持久化记录，再点击一次 Place order。
  7. 读取 Verify your payment 中 name=threeDSIframe 的框架；识别 Bybit App 提示并核对金额，
     写入 awaiting_bybit_authorization、发 Mac 本地通知，停止点击，等待本人手机授权。
     不点 Receive Email、Cancel payment、重试或授权按钮。
  是否要求 3DS 由商户/发卡行决定，程序不能强制每笔都等待手机。
  拒付、取消、超时、未知跳转及结果不明都停止，不自动重试。

持久化保护与资料
  .private/iab-run/running.lock               执行器互斥锁
  .private/iab-run/cart-<product>-<sku>.json   加购前落盘
  .private/iab-run/submit-<product>-<sku>.json 提交前独占创建并 fsync
  .private/iab-run/verified-cart.json         本程序核对 PDP SKU 后的购物袋凭据
  .private/iab-run/status.json                阶段、金额、卡尾号，不含完整支付资料或地址
  .private/iab-settings.json                 本人已提供的邮箱和已核对地址标签，仅用于官网
  记录不会自动删除；中断后须核对官网订单和 Bybit，不能盲目删锁重跑。

只读检查
  .venv/bin/python lululemon_checkout.py --check

Python 监控并交接（不会自行启动）
  .venv/bin/python lululemon_checkout.py --watch \
    --start 2026-11-17T04:00:00+08:00 \
    --end 2026-11-17T08:00:00+08:00 --interval 30
  请求失败会退避并尊重 Retry-After；接口失败不等于无货。
  找到目标后写 checkout_data/candidate.json，等待内置浏览器执行器。

内置浏览器运行环境中的调用示例
  // tab 是官方浏览器工具返回的现有 IAB Tab，不是普通 node 的全局变量。
  const runner = await import('file://<project-dir>/iab_runner.mjs');
  // 当前事件处理只允许这一入口：expectedSku来自availability_notifier.py handoff真实输出。
  await runner.preparePaymentOnce(tab,{expectedSku,deadlineAt:Date.parse('2026-11-16T22:00:00Z')});

  // 以下是旧Review测试用法，不属于当前事件处理：
  const result = await runner.runOnce(tab, {dryRun:true, maxTotal:169});

  await runner.monitorTick(tab, {dryRun:true, maxTotal:169,
    start:'2026-11-17T04:00:00+08:00', end:'2026-11-17T08:00:00+08:00', intervalSeconds:30});
  开始前返回 scheduled，间隔不足返回 cooldown，结束后返回 window_finished。
  候选执行、异常或付款交接后停止调度。真实提交须显式 dryRun:false 并提供已获得的精确授权。
  电脑、网络及浏览器运行环境须保持可用；没有持续调度就没有无人值守监控。
  当前使用真实事件触发Gmail发给QQ和163，状态见availability_notifier.py status；另有Mac本地通知，没有手机确认网页。

测试
  .venv/bin/python -m unittest -v test_lululemon_monitor test_checkout test_checkout_ui test_checkout_runner
  node --test test_iab_checkout.mjs
  Node 测试复用 Python Playwright 内附驱动，浏览器全部请求在本机拦截，不访问真实付款接口。
  覆盖错误商品/尺码/数量/卡/总价、过期库存、未授权、未保存卡、并发/重启防重复、3DS 框架、
  金额不符、验证失败、空白结果超时、执行窗口结束。

真实验证结果
  旧 Chrome 收货提交为 HTTP 400 / GE401001，原因未知。
  内置浏览器保持登录，通过收货和手动填卡，Review 显示 Mastercard 0000。
  用户单独授权 Oversized / Lavender Rock / US S / 1 件 / A$169 的真实提交测试。
  元素定位点击一次 Place order 后实际到达 Bybit App 3DS 授权提示。
  用户要求 Cancel payment 后已取消，官网显示 Authentication failed，随后返回填卡步骤。
  未再次提交，未观察到订单成功或付款成功；不证明原目标 Classic Lavender Rock 已上架。
  当前止于填卡页，是否保存0000不影响此阶段；本人手动填写。
  真实自动购买未启动。脚本链使用Gmail事件通知，无需SMTP；实际运行状态见notification_chain.py status。

2026-11-16真实对照测试：Classic Black / US XS / SKU159402216；QQ测试邮件Gmail已发送。
  使用元素自动化达到Payment并观察到信用卡输入框，未填卡或提交。已移除测试品并恢复空购物袋。
  修复内置运行环境无process全局、商品页加载重置尺码、收货控件需等待三个问题。
  本次没有重新走跨进程queue触发，不能把它报告成全新无人值守端到端唤醒测试。
