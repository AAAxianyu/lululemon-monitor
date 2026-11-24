最新状态（2026-11-17 00:37 北京，Claude 接力）：
  direct_checkout.mjs 改为 prepareCheckout(deferShipping:true)：CDP Chrome 完成加购/联系/地址核对，
  然后调用 native_shipping.py 在普通 Chrome（同一 checkout-chrome-manual-login 配置）原生点收货 Continue，停在 Payment。
  原生点击需要 macOS 辅助功能权限；若无权限，会停在普通 Chrome 的收货页，由本人点 Continue 再填卡。
  黑色 XS 对照实测（run-id claude-0035）：加购到收货页约 57 秒；Claude 的 shell 没有辅助功能权限，因此停在收货页。
  测试件已删除，购物袋为空；direct-browser-ready.json=true，浏览器 worker 处于 waiting。
  01:01 更新：普通 Chrome 启动时加 --force-renderer-accessibility（不加的话网页内容要 60–150 秒以上才对辅助功能可见）；
  原生 helper 已按源码重新编译（之前的二进制没有 radios/checkboxes 字段）；验证时排除浏览器标签栏的 radio，未渲染完就继续轮询。
  黑色 XS 完整实测（run-id claude-0100）：空购物袋→加购→普通 Chrome 收货 Continue→Payment，共 39 秒，未填卡、未提交；测试件已删除。

  01:12 更新（用户决定）：Lavender Rock 带拉链的组合色（如 Lavender Rock/Gold、Lavender Rock/Mirror Silver，色号可能不是 77631）也算目标。
  邮件：颜色名或色组含 Lavender Rock/LAVR 就发。自动到填卡页：颜色名=Lavender Rock 或 Lavender Rock/<拉链色>，且色组以 LAVR 开头，
  并且唯一 XS 可售才执行；按官网返回的实际颜色名/SKU 核对。多个候选时只发邮件，不加购。监控链已重启（pid 38767）。

最新状态（2026-11-16）：用户要求切换独立浏览器脚本。
  当前配置 browser_executor=direct，由 direct_browser_worker.py 直接启动 direct_checkout.mjs；无聊天/queue。
  QQ SMTP直发及0.1秒检测保持待命。专用Chrome与内置浏览器会话独立。
  真实黑色XS测试尚待登录成功；direct-browser-ready.json=false，正式结账暂未启用。
  direct_checkout.mjs只调用prepareCheckout(stopAt=payment)，不调用Review或提交入口。
  下文IAB说明保留作历史记录，以此最新状态及运行状态文件为准。

lululemon 澳洲监控、Gmail 双邮箱通知和填卡准备

本次窗口和精确目标
  北京时间 2026-11-17 00:50–06:00；UTC 2026-11-16 16:50–22:00。
  这些是用户指定的观察时点，不是官网确认的上架时间。
  lopmi7nqah / Scuba Evolve Classic Full-Zip Hoodie / Lavender Rock / LAVR / 77631。
  US XS（AU 6–8），一件。不同剪裁、其他尺码、组合色均不替代。
  邮件收件人：alerts@example.com、recipient2@example.com、recipient3@example.com。

脚本链（2026-11-16 已切换 QQ SMTP）
  notification_chain.py 管理检测、SMTP直发、浏览器事件三个独立进程。
  availability_notifier.py：官网商品及库存HTTP检测。
  smtp_notifier.py：轮询本地已确认事件，预热TLS连接，直接发往QQ和163，无需聊天响应。
  event_dispatcher.py：仅XS可售事件调用一次codex queue，启动内置浏览器到填卡页。
  单独listed事件只由SMTP通知，不唤醒聊天；聊天不再调用Gmail或重复发送邮件。
  每个收件人独立持久记录；部分失败不重发成功者，DATA结果不明不盲重试。
  子进程异常最多重启三次；无周期聊天唤醒，不是开机服务。

检测方式
  整个窗口每0.1秒尝试发起一轮，两条已核实 GET 并行：
  https://www.lululemon.com.au/.uf-svc/v1/products/lopmi7nqah
  https://www.lululemon.com.au/.uf-svc/v1/products/lopmi7nqah/skus/availability?fulfillment=ship
  最多10轮（20个HTTP请求）同时进行；满载就等空位，不补发错过的轮次。
  请求发起间隔不等于响应速度、邮件送达时间或网页准备完成时间。
  429、403、网络或解析失败至少退避30秒，连续失败指数退避至15分钟；遵守更长的Retry-After。
  不轮换IP、不绕过限制；旧的在途成功响应不能撤销退避。
  只有最新发起序号的响应参与状态判断，旧响应保留为证据。
  发现正向结果后停止追加，排空在途请求并再查一次；两次一致后才生成持久事件。
  listed：精确款式和颜色出现真实数字SKU，先通知；XS未确认可售时不自动加购。
  xs_available：唯一US XS及独立库存接口isAvailable=true，再通知并触发填卡准备。
  首次发现就有XS时只产生XS事件；同阶段不重复生成。颜色元数据、图片、URL参数不算上架。
  XS确认后停止检测。06:00之后不新查官网、不开始结账；邮件可处理既有事件至06:10。
  仅监控已知商品ID；不能据此排除官网用全新ID重新建商品。

邮件发送
  发件人alerts@example.com，smtp.qq.com:465，TLS；授权码存本机钥匙串。
  正式listed和xs_available事件发送给alerts@example.com、recipient2@example.com及recipient3@example.com。
  邮件模板预先准备；本地事件每0.05秒检查，观察窗口开始前30秒预热连接。
  Gmail连接已退出当前通知链；邮件发送不等待聊天或浏览器动作。
  测试只发QQ；本次测试SMTP已接受，连接认证0.422秒，发信0.565秒，总计0.992秒。
  服务器接受不等于收件箱收到，实际收件等待用户确认。
  测试结果.private/availability-notifier/qq-smtp-test.json，重复运行不会重复发送。

自动到填卡页
  SMTP邮件与浏览器准备由同一真实事件分别触发；浏览器不等待邮件发送。
  浏览器仍通过已加载的当前Codex聊天和内置浏览器运行；本地Python不能直接连接IAB。
  当前聊天需保持可用，IAB已有登录及已核对地址需仍有效。App关闭或聊天卸载的恢复未验证。
  调用preparePaymentOnce()，复查库存与事件SKU、PDP实际颜色/尺码/SKU，然后加购唯一目标。
  原Oversized / Lavender Rock / US S在Saved Items，不属于购物袋；实测当前购物袋为空。
  收货复用已核对地址，不重写地址；Authority to leave package without signature设为No。
  到Payment核对订单商品后硬停止：不选保存卡、不填卡、不点付款Continue，不勾条款，不点Place order。
  本人手动填写Bybit尾号0000。旧的一次真实提交测试授权不能复用。
  每个动作前检查截止时间及暂停标记；登录、地址、库存或页面不符即停下。
  持久prepare/cart记录防止重复加购，异常不擅自删除记录后重跑。

运行命令（项目目录下）
  .venv/bin/python notification_chain.py status
  .venv/bin/python notification_chain.py resume # 仅在用户已授权恢复后执行
  .venv/bin/python notification_chain.py stop   # 写共享暂停标记，终止子进程
  .venv/bin/python event_dispatcher.py status
  .venv/bin/python availability_notifier.py status
  .venv/bin/python availability_notifier.py handoff

实际运行状态以.private/availability-notifier/chain-status.json和进程锁为准。
程序开始前只等待，不提前查询；caffeinate -i防止空闲睡眠，但合盖/断网/关机仍会打断运行。
HTTP原文、地址、状态码、时间、哈希在monitor_data/notifications/。
持久SQLite保存事件、分收件人SMTP投递状态及消息ID；不把收货地址、银行卡或授权码写进邮件。

验证与边界
  既有Gmail→QQ测试已经用户确认收到；163实际收件仍未确认。
  0.1秒只进行过短突发HTTP测试，未发现429/403，不保证5小时连续运行不会限速。
  新版离线测试覆盖精确商品、并发/乱序/退避、双收件人、部分发送失败、崩溃去重、暂停及Payment硬停止。
  浏览器全部支付测试在本地拦截网络的测试页面完成，不触达真实付款服务。
  原IAB真实结账曾经由用户参与到Payment/Review；本次真实Classic Black/US XS对照测试已通过检测、Gmail发送及Payment填卡准备；正式Lavender Rock尚未实跑。
  正式目标仍严格匹配Lavender Rock；只有用户单独授权时才能用其他颜色做隔离测试。

2026-11-16真实对照测试：Classic Black / US XS / SKU159402216；QQ测试邮件Gmail已发送。
  使用元素自动化达到Payment并观察到信用卡输入框，未填卡或提交。已移除测试品并恢复空购物袋。
  修复内置运行环境无process全局、商品页加载重置尺码、收货控件需等待三个问题。
  本次没有重新走跨进程queue触发，不能把它报告成全新无人值守端到端唤醒测试。
