"use client";

import { ChevronsDownUp, ChevronsUpDown } from "lucide-react";
import { createContext, type ReactNode, useContext, useId, useState } from "react";

type CardState = { defaultOpen?: boolean; cards: Record<string, boolean> };
const CardContext = createContext<{
  state: CardState;
  setCardOpen: (id: string, open: boolean) => void;
} | null>(null);

export function ResponseCardGroup({ children, showControls = true }: {
  children: ReactNode;
  showControls?: boolean;
}) {
  const [state, setState] = useState<CardState>({ cards: {} });
  const allOpen = state.defaultOpen ?? true;

  return (
    <CardContext.Provider value={{
      state,
      setCardOpen: (id, open) => setState((current) => ({
        ...current, cards: { ...current.cards, [id]: open },
      })),
    }}>
      {showControls ? (
        <div className="response-card-controls" role="group" aria-label="本轮回复卡片控制">
          <button
            type="button"
            aria-label={allOpen ? "折叠本轮全部卡片" : "展开本轮全部卡片"}
            onClick={() => setState({ defaultOpen: !allOpen, cards: {} })}
          >
            {allOpen ? <ChevronsDownUp size={14} aria-hidden="true" /> : <ChevronsUpDown size={14} aria-hidden="true" />}
            {allOpen ? "全部折叠" : "全部展开"}
          </button>
        </div>
      ) : null}
      {children}
    </CardContext.Provider>
  );
}

export function useResponseCard(defaultOpen = true) {
  const id = useId();
  const context = useContext(CardContext);
  const [localOpen, setLocalOpen] = useState<boolean>();
  const open = context
    ? context.state.cards[id] ?? context.state.defaultOpen ?? defaultOpen
    : localOpen ?? defaultOpen;

  function setOpen(nextOpen: boolean) {
    // 原生 details 也会为程序更新触发 toggle；只记录用户实际改变的状态。
    if (nextOpen === open) return;
    if (context) context.setCardOpen(id, nextOpen);
    else setLocalOpen(nextOpen);
  }

  return [open, setOpen] as const;
}
