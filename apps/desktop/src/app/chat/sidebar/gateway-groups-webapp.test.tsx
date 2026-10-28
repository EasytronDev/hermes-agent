// @vitest-environment jsdom
import { act, cleanup, render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, expect, it } from 'vitest'

import { SidebarProvider } from '@/components/ui/sidebar'
import { $connectionsRegistry } from '@/store/connection-registry-state'
import { setSidebarGrouping } from '@/store/layout'
import { $profiles, $showAllProfiles } from '@/store/profile'
import { $sessions } from '@/store/session'
import { makeSessionInfo } from '@/test/session-info'

import { ChatSidebar } from './index'

const noop = () => {}

const noopAsync = async () => {}

const mount = () =>
  render(
    <MemoryRouter>
      <SidebarProvider>
        <ChatSidebar
          currentView="chat"
          onArchiveSession={noop}
          onBranchSession={noop}
          onDeleteSession={noop}
          onLoadMoreSessions={noop}
          onManageCronJob={noop}
          onNavigate={noop}
          onNewSessionInWorkspace={noop}
          onNewSessionSplit={noop}
          onResumeSession={noop}
          onRetrySessions={noopAsync}
          onTriggerCronJob={async () => {}}
        />
      </SidebarProvider>
    </MemoryRouter>
  )

afterEach(() => {
  cleanup()
  $showAllProfiles.set(false)
  $sessions.set([])
})

// The Webapp publishes no connection registry: its one gateway is the `local` source.
it('names the Webapp gateway section after the host instead of its raw `local` id', () => {
  mount()
  act(() => {
    $connectionsRegistry.set(null)
    $profiles.set([
      { name: 'default', is_default: true },
      { name: 'work', is_default: false }
    ] as typeof $profiles.value)
    $showAllProfiles.set(true)
    setSidebarGrouping('profile')
    $sessions.set(
      [
        ['listed', 'default'],
        ['new-chat', 'default'],
        ['work-chat', 'work']
      ].map(([id, profile]) =>
        makeSessionInfo({ id, connection_id: 'local', profile, title: `${id} title`, last_active: Date.now() / 1000 })
      )
    )
  })

  const sections = document.querySelectorAll('[data-gateway-section]')
  expect(sections).toHaveLength(1)
  const gateway = sections[0] as HTMLElement
  expect(within(gateway).getByText('Hermes host')).toBeTruthy()
  expect(screen.queryByText('local')).toBeNull()
  expect(gateway.querySelectorAll('[data-gateway-group]')).toHaveLength(2)
  expect(within(gateway).getByText('listed title')).toBeTruthy()
  expect(within(gateway).getByText('new-chat title')).toBeTruthy()
  expect(within(gateway).getByText('work-chat title')).toBeTruthy()
})
