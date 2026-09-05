import { useEffect, useRef } from "react";
import { createPortal } from "react-dom";
import "./agent-profile.css";

/** Native modal keeps focus and Escape behavior local to the selected person. */
export function AgentProfile({ children, onClose }) {
  const dialog = useRef(null);
  const close = useRef(onClose);
  const backdropPress = useRef(false);
  close.current = onClose;
  useEffect(() => {
    const element = dialog.current;
    const previous = document.activeElement;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    if (element.showModal) element.showModal();
    else element.setAttribute("open", "");
    return () => {
      element.close?.();
      document.body.style.overflow = overflow;
      if (previous?.isConnected) previous.focus?.({ preventScroll: true });
    };
  }, []);
  return createPortal(
    <dialog
      ref={dialog}
      className="agent-profile"
      aria-labelledby="agent-profile-name"
      onCancel={(event) => {
        event.preventDefault();
        close.current();
      }}
      onKeyDown={(event) => {
        if (event.key !== "Tab") return;
        const stops = [
          ...event.currentTarget.querySelectorAll(
            'button:not(:disabled),a[href],input:not(:disabled),select:not(:disabled),textarea:not(:disabled),summary,[tabindex]:not([tabindex="-1"])',
          ),
        ].filter(
          (element) =>
            element.getClientRects().length > 0 && !element.closest("[inert]"),
        );
        if (!stops.length) {
          event.preventDefault();
          return;
        }
        const first = stops[0],
          last = stops.at(-1);
        if (
          event.shiftKey &&
          (document.activeElement === first ||
            document.activeElement === event.currentTarget)
        ) {
          event.preventDefault();
          last.focus();
        } else if (
          !event.shiftKey &&
          (document.activeElement === last ||
            document.activeElement === event.currentTarget)
        ) {
          event.preventDefault();
          first.focus();
        }
      }}
      onPointerDown={(event) => {
        const rect = event.currentTarget.getBoundingClientRect();
        backdropPress.current =
          event.target === event.currentTarget &&
          (event.clientX < rect.left ||
            event.clientX > rect.right ||
            event.clientY < rect.top ||
            event.clientY > rect.bottom);
      }}
      onPointerCancel={() => {
        backdropPress.current = false;
      }}
      onClick={(event) => {
        const anchor = event.target.closest?.('a[href^="#"]');
        if (anchor && event.currentTarget.contains(anchor)) {
          close.current();
          return;
        }
        const startedOutside = backdropPress.current;
        backdropPress.current = false;
        if (!startedOutside || event.target !== event.currentTarget) return;
        const rect = event.currentTarget.getBoundingClientRect();
        if (
          event.clientX < rect.left ||
          event.clientX > rect.right ||
          event.clientY < rect.top ||
          event.clientY > rect.bottom
        )
          close.current();
      }}
    >
      <button
        className="agent-profile-close"
        type="button"
        aria-label="Close villager details"
        onClick={() => close.current()}
      >
        ×
      </button>
      <div className="agent-profile-content">{children}</div>
    </dialog>,
    document.body,
  );
}
