import { createContext, useContext } from 'react';

export interface CanvasActions {
  toggleExpand: (nodeId: string) => void;
  startRename: (nodeId: string) => void;
  finishRename: (nodeId: string, name: string) => void;
  cancelRename: () => void;
  renamingNodeId: string | null;
  deleteNode: (nodeId: string) => void;
  detachAccount: (clientId: string, accountId: string) => void;
  moveAccountToClient: (srcClientId: string, accountId: string, dstClientId: string) => void;
  detachLogin: (clientId: string, utility: 'GMP' | 'VEC' | 'WEC', originClientId?: number | null, loginId?: string | null) => void;
  moveLoginToClient: (
    srcClientId: string,
    utility: 'GMP' | 'VEC' | 'WEC',
    dstClientId: string,
    originClientId?: number | null,
    loginId?: string | null,
  ) => void;
  /** Look up an origin client by id — used to label moved logins
   *  ("from Marie's GMP login"). Returns null when the origin is unknown or
   *  the lookup hasn't loaded yet. */
  getOriginClient: (clientId: number) => {
    id: number;
    name: string;
    deleted: boolean;
    logins: { GMP?: string | null; VEC?: string | null; WEC?: string | null };
  } | null;
  /** Toggle the pinned/starred state of a client. */
  togglePin: (clientId: string) => void;
}

export const CanvasActionsContext = createContext<CanvasActions | null>(null);

export function useCanvasActions(): CanvasActions {
  const ctx = useContext(CanvasActionsContext);
  if (!ctx) throw new Error('useCanvasActions must be used inside CanvasActionsContext.Provider');
  return ctx;
}
