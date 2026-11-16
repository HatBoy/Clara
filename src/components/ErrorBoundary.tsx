import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
}

/**
 * 顶层错误边界（2026-11-16 优化批 1）：任一组件在 render 期因异常 payload
 * （日期 NaN、proposal 形状不符等）抛错时，避免整个 WebView 白屏——显示
 * 兜底页与「重新加载」入口。不引第三方库，改动面最小。
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // 保留栈信息供用户反馈/开发排查（WebView 控制台可读）
    console.error("界面渲染异常", error, info.componentStack);
  }

  render() {
    if (this.state.error) {
      return (
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            justifyContent: "center",
            height: "100vh",
            gap: 12,
            fontFamily: "inherit",
          }}
        >
          <div style={{ fontSize: 15, fontWeight: 600 }}>界面出现异常</div>
          <div style={{ fontSize: 12, opacity: 0.7, maxWidth: 420, textAlign: "center" }}>
            {this.state.error.message || "未知错误"}
          </div>
          <button
            type="button"
            onClick={() => window.location.reload()}
            style={{ padding: "6px 14px", fontSize: 13, cursor: "pointer" }}
          >
            重新加载
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
