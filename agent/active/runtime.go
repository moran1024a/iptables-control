package main

import (
	"context"
	"sync"
	"time"
)

// Only maintenance is preemptible. Once a rule transaction starts committing,
// it finishes (or rolls back) before another configuration can run.
type maintenanceWork struct {
	mu      sync.Mutex
	cancel  context.CancelFunc
	pending int
}

func (w *maintenanceWork) foreground() func() {
	w.mu.Lock()
	w.pending++
	if w.cancel != nil {
		w.cancel()
	}
	w.mu.Unlock()
	return func() { w.mu.Lock(); w.pending--; w.mu.Unlock() }
}
func (w *maintenanceWork) begin(parent context.Context) (context.Context, func(), bool) {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.pending != 0 {
		return nil, nil, false
	}
	ctx, cancel := context.WithTimeout(parent, 5*time.Second)
	w.cancel = cancel
	return ctx, func() { w.mu.Lock(); cancel(); w.cancel = nil; w.mu.Unlock() }, true
}
func (a *Agent) initRuntime() {
	if a.stateMu == nil {
		a.stateMu = &sync.Mutex{}
	}
	a.stateMu.Lock()
	defer a.stateMu.Unlock()
	if a.maintenance == nil {
		a.maintenance = &maintenanceWork{}
	}
}
func (a *Agent) preparationContext() context.Context {
	if a.prepareCtx != nil {
		return a.prepareCtx
	}
	return context.Background()
}
func (a *Agent) prepareCommand(input []byte, name string, args ...string) ([]byte, error) {
	ctx, cancel := context.WithTimeout(a.preparationContext(), 20*time.Second)
	defer cancel()
	return command(ctx, input, name, args...)
}

type dnsResult struct {
	ip      string
	expires time.Time
}

func (a *Agent) snapshot() Agent {
	a.stateMu.Lock()
	defer a.stateMu.Unlock()
	copy := *a
	copy.usage = nil
	copy.dns = nil
	copy.prepareCtx = nil
	return copy
}

type outgoingMessage struct {
	body   M
	result chan error
}

// One writer, with configuration/liveness receipts ahead of telemetry. Queues
// are bounded; an already-started WebSocket frame is never interrupted.
func messageWriter(ctx context.Context, write func(M) error) (func(M) error, func(M) error) {
	control, reports := make(chan outgoingMessage, 8), make(chan outgoingMessage, 1)
	go func() {
		for {
			var item outgoingMessage
			select {
			case <-ctx.Done():
				return
			case item = <-control:
			default:
				select {
				case <-ctx.Done():
					return
				case item = <-control:
				case item = <-reports:
				}
			}
			item.result <- write(item.body)
		}
	}()
	submit := func(queue chan outgoingMessage, body M) error {
		item := outgoingMessage{body, make(chan error, 1)}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case queue <- item:
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case err := <-item.result:
			return err
		}
	}
	return func(body M) error { return submit(control, body) }, func(body M) error { return submit(reports, body) }
}
