import { useEffect, useState, useCallback } from 'react'
import { useTranslation } from 'react-i18next'

import homeStore from '@/features/stores/home'
import settingsStore from '@/features/stores/settings'
import webSocketStore from '@/features/stores/websocketStore'
import { EmotionType } from '@/features/messages/messages'
import { useRestrictedMode } from '@/hooks/useRestrictedMode'

///取得したコメントをストックするリストの作成（receivedMessages）
interface TmpMessage {
  text: string
  role: string
  emotion: EmotionType
  type: string
  image?: string
}

interface Params {
  handleReceiveTextFromWs: (
    text: string,
    role?: string,
    emotion?: EmotionType,
    type?: string,
    image?: string
  ) => Promise<void>
}

const useExternalLinkage = ({ handleReceiveTextFromWs }: Params) => {
  const { t } = useTranslation()
  const { isRestrictedMode } = useRestrictedMode()
  const externalLinkageMode = settingsStore((s) => s.externalLinkageMode)
  const [receivedMessages, setTmpMessages] = useState<TmpMessage[]>([])
  const websocketUrl = 'ws://localhost:8000/ws'

  const processMessage = useCallback(
    async (message: TmpMessage) => {
      await handleReceiveTextFromWs(
        message.text,
        message.role,
        message.emotion,
        message.type,
        message.image
      )
    },
    [handleReceiveTextFromWs]
  )

  useEffect(() => {
    if (receivedMessages.length > 0) {
      const message = receivedMessages[0]
      const processedMessage =
        message.role === 'output' ||
        message.role === 'executing' ||
        message.role === 'console'
          ? { ...message, role: 'code' }
          : message
      setTmpMessages((prev) => prev.slice(1))
      processMessage(processedMessage)
    }
  }, [receivedMessages, processMessage])

  useEffect(() => {
    const ss = settingsStore.getState()
    if (!ss.externalLinkageMode || isRestrictedMode) {
      console.log(
        'External linkage WebSocket skipped:',
        JSON.stringify({
          externalLinkageMode: ss.externalLinkageMode,
          isRestrictedMode,
        })
      )
      return
    }

    const handleOpen = (event: Event) => {
      console.log('External linkage WebSocket opened:', event)
    }
    const handleMessage = async (event: MessageEvent) => {
      console.log('External linkage WebSocket message:', event.data)
      const jsonData = JSON.parse(event.data)
      setTmpMessages((prevMessages) => [...prevMessages, jsonData])
    }
    const handleError = (event: Event) => {
      console.error('External linkage WebSocket error:', event)
    }
    const handleClose = (event: Event) => {
      console.log('External linkage WebSocket closed:', event)
    }

    const handlers = {
      onOpen: handleOpen,
      onMessage: handleMessage,
      onError: handleError,
      onClose: handleClose,
    }

    function connectWebsocket() {
      const wsManager = webSocketStore.getState().wsManager
      if (wsManager?.isConnected()) return wsManager.websocket
      console.log(`External linkage WebSocket connecting to ${websocketUrl}`)
      return new WebSocket(websocketUrl)
    }

    webSocketStore.getState().initializeWebSocket(t, handlers, connectWebsocket)

    const reconnectInterval = setInterval(() => {
      const ss = settingsStore.getState()
      const wsManager = webSocketStore.getState().wsManager
      if (
        ss.externalLinkageMode &&
        wsManager?.websocket &&
        wsManager.websocket.readyState !== WebSocket.OPEN &&
        wsManager.websocket.readyState !== WebSocket.CONNECTING
      ) {
        homeStore.setState({ chatProcessing: false })
        console.log('External linkage WebSocket reconnecting...')
        wsManager.disconnect()
        webSocketStore
          .getState()
          .initializeWebSocket(t, handlers, connectWebsocket)
      }
    }, 2000)

    return () => {
      clearInterval(reconnectInterval)
      webSocketStore.getState().disconnect()
    }
  }, [externalLinkageMode, isRestrictedMode, t])

  return null
}

export default useExternalLinkage
