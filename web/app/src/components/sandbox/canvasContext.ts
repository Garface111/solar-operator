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
}

export const CanvasActionsContext = createContext<CanvasActions | null>(null);

export function useCanvasActions(): CanvasActions {
  const ctx = useContext(CanvasActionsContext);
  if (!ctx) throw new Error('useCanvasActions must be used inside CanvasActionsContext.Provider');
  return ctx;
}
