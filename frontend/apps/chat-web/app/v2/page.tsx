"use client";

import dynamic from "next/dynamic";

const ChatV2PageClient = dynamic(() => import("./page-client"), {
  ssr: false,
  loading: () => (
    <div className="shell">
      <header className="masthead">
        <div className="brand-copy">
          <div className="brand-headline">
            <p className="eyebrow">Intent Router / Chat V2</p>
            <h1>动态图编排会话台</h1>
          </div>
          <p className="masthead-copy">正在初始化 V2 会话与执行图环境。</p>
        </div>
      </header>
      <main className="workspace">
        <section className="conversation-stage">
          <header className="stage-topline">
            <div>
              <p className="section-label">会话区</p>
              <h2>多轮对话</h2>
            </div>
          </header>
          <div className="message-list" aria-live="polite">
            <article className="message">
              <div className="meta">系统</div>
              <p>正在建立会话，请稍候。</p>
            </article>
          </div>
          <div className="composer">
            <label className="composer-label" htmlFor="chat-v2-loading-composer">
              输入消息
            </label>
            <textarea id="chat-v2-loading-composer" disabled placeholder="正在加载 V2..." />
          </div>
        </section>
        <aside className="context-rail">
          <section className="rail-section">
            <div className="section-head">
              <p className="section-label">图状态</p>
            </div>
            <h3>初始化中</h3>
            <p className="status-copy">客户端加载完成后会建立会话、订阅事件并恢复执行图视图。</p>
          </section>
        </aside>
      </main>
    </div>
  ),
});

export default function ChatV2Page() {
  return <ChatV2PageClient />;
}
