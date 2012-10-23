from __future__ import absolute_import

import logging
import sys
import traceback

import greenhouse
from . import connection, const
from .. import errors, hooks


log = logging.getLogger("junction.dispatch")


class Dispatcher(object):
    def __init__(self, rpc_client, hub, hooks=None):
        self.rpc_client = rpc_client
        self.hub = hub
        self.hooks = hooks
        self.peer_subs = {}
        self.local_subs = {}
        self.peers = {}
        self.reconnecting = {}
        self.inflight_proxies = {}

    def add_local_subscription(self, msg_type, service, mask, value, method,
            handler, schedule):
        # storage in local_subs is shaped like so:
        # {(msg_type, service): [
        #     (mask, value, {method: (handler, schedule), ...}), ...], ...}

        # sanity check that no 1 bits in the value would be masked out.
        # in that case, there is no routing id that could possibly match
        if value & ~mask:
            raise errors.ImpossibleSubscription(msg_type, service, mask, value)

        existing = self.local_subs.setdefault((msg_type, service), [])
        for pmask, pvalue, phandlers in existing:
            if pmask & value == mask & pvalue:
                if method in phandlers:
                    # (mask, value) overlaps with a previous
                    # subscription with the same method
                    raise errors.OverlappingSubscription(
                            (msg_type, service, mask, value, method),
                            (msg_type, service, pmask, pvalue, method))
                elif mask == pmask and value == pvalue:
                    # same (mask, value) as a previous subscription but for a
                    # different method, so piggy-back on that data structure
                    phandlers[method] = (handler, schedule)

                    # also bail out. we can skip the MSG_TYPE_ANNOUNCE
                    # below b/c peers don't route with their peers' methods
                    return

        existing.append((mask, value, {method: (handler, schedule)}))

        # let peers know about the new subscription
        for peer in self.peers.itervalues():
            if not peer.up:
                continue
            peer.push((const.MSG_TYPE_ANNOUNCE,
                    (msg_type, service, mask, value)))

    def remove_local_subscription(
            self, msg_type, service, mask, value):
        group = self.local_subs.get((msg_type, service), 0)
        if not group:
            return False
        for i, (pmask, pvalue, phandlers) in enumerate(group):
            if (mask, value) == (pmask, pvalue):
                del group[i]
                if not group:
                    del self.local_subs[(msg_type, service)]
                for peer in self.peers.itervalues():
                    if not peer.up:
                        continue
                    peer.push((const.MSG_TYPE_UNSUBSCRIBE,
                        (msg_type, service, mask, value)))
                return True
        return False

    def incoming_unsubscribe(self, peer, msg):
        if not isinstance(msg, tuple) or len(msg) != 5:
            # badly formatted message
            log.warn("received malformed unsubscribe from %r" % (peer.ident,))
            return

        log.debug("received unsubscribe %r from %r" % (msg, peer.ident))

        msg_type, service, mask, value = msg

        groups = self.peer_subs.get((msg_type, service), 0)
        if not groups:
            log.warn(("unsubscribe from %r described an unrecognized" +
                    " subscription (msg_type, service)") % (peer.ident,))
            return
        for i, group in enumerate(groups):
            if (mask, value, peer) == group:
                del groups[i]
                if not groups:
                    del self.peer_subs[(msg_type, service)]
                break
        else:
            log.warn(("unsubscribe from %r described an " +
                    "unrecognized subscription %r") % (peer.ident, msg))

    def find_local_handler(self, msg_type, service, routing_id, method):
        group = self.local_subs.get((msg_type, service), 0)
        if not group:
            return None, False
        for mask, value, handlers in group:
            if routing_id & mask == value and method in handlers:
                return handlers[method]
        return None, False

    def locally_handles(self, msg_type, service, routing_id):
        group = self.local_subs.get((msg_type, service), [])
        if not group:
            return False
        for mask, value, handlers in group:
            if routing_id & mask == value:
                return True
        return False

    def local_subscriptions(self):
        for key, value in self.local_subs.iteritems():
            msg_type, service = key
            for mask, value, handlers in value:
                yield (msg_type, service, mask, value)

    def add_reconnecting(self, addr, peer):
        self.reconnecting[addr] = peer

    def store_peer(self, peer, subscriptions):
        loser = None
        if peer.ident in self.peers:
            winner, loser = connection.compare(peer, self.peers[peer.ident])
            if peer is loser:
                winner.established.set()
                peer.established.set()
                return False
        elif peer.ident in self.reconnecting:
            log.info("terminating reconnect loop in favor of incoming conn")
            loser = self.reconnecting.pop(peer.ident)

        peer.established.set()
        if loser is not None:
            loser.established.set()
            loser.go_down(reconnect=False, expected=True)

        self.peers[peer.ident] = peer
        self.add_peer_subscriptions(peer, subscriptions)
        return True

    def connection_lost(self, peer, subs):
        hooks._get(self.hooks, "connection_lost")(
                self.hub, peer.ident, subs)

    def drop_peer(self, peer):
        self.peers.pop(peer.ident, None)
        subs = self.drop_peer_subscriptions(peer)

        # reply to all in-flight proxied RPCs to the dropped peer
        # with the "lost connection" error
        for counter in self.rpc_client.by_peer.get(id(peer), []):
            if counter in self.inflight_proxies:
                self.proxied_response(counter, const.RPC_ERR_LOST_CONN, None)

        self.rpc_client.connection_down(peer)
        return subs

    def incoming_announce(self, peer, msg):
        if not isinstance(msg, tuple) or len(msg) != 4:
            # drop malformed messages
            log.warn("received malformed announce from %r" % (peer.ident,))
            return

        log.debug("received announce %r from %r" % (msg, peer.ident))

        self.add_peer_subscriptions(peer, [msg])

    def add_peer_subscriptions(self, peer, subscriptions):
        # format for peer_subs:
        # {(msg_type, service): [(mask, value, connection)]}
        for msg_type, service, mask, value in subscriptions:
            self.peer_subs.setdefault((msg_type, service), []).append(
                    (mask, value, peer))

    def drop_peer_subscriptions(self, peer):
        removed = []
        for (msg_type, service), subs in self.peer_subs.items():
            for (mask, value, conn) in subs:
                if conn is peer:
                    removed.append((msg_type, service, mask, value))
                    subs.remove((mask, value, conn))
            if not subs:
                del self.peer_subs[(msg_type, service)]
        return removed

    def find_peer_routes(self, msg_type, service, routing_id):
        for mask, value, peer in self.peer_subs.get((msg_type, service), []):
            if peer.up and routing_id & mask == value:
                yield peer

    def send_publish(self, service, routing_id, method, args, kwargs,
            forwarded=False, singular=False):
        # get the peers registered for this publish
        peers = list(self.find_peer_routes(
                const.MSG_TYPE_PUBLISH, service, routing_id))

        # handle locally if we have a hander for it
        handler, schedule = self.find_local_handler(
                const.MSG_TYPE_PUBLISH, service, routing_id, method)

        targets = peers[:]
        if handler:
            targets.append(LocalTarget(self, handler, schedule))

        if singular:
            targets = [self.target_selection(
                targets, service, routing_id, method)]
            if not isinstance(targets[0], LocalTarget):
                handler = None

        if len(args) == 1 and hasattr(args[0], "__iter__") \
                and not hasattr(args[0], "__len__"):
            self.send_chunked_publish(service, routing_id, method, args[0],
                    kwargs, targets, proxied=False)
            return bool(handler or peers)

        msg = (const.MSG_TYPE_PUBLISH,
                (service, routing_id, method, args, kwargs))

        if handler is not None:
            log.debug("locally handling publish %r %s" %
                    (msg[1][:3], "scheduled" if schedule else "immediately"))

        if peers and not (singular and handler):
            log.debug("sending publish %r to %d peers" % (
                msg[1][:3], len(peers)))

        for target in targets:
            target.push(msg)

        return bool(handler or peers)

    def send_chunked_publish(self, service, routing_id, method,
            chunks, kwargs, targets, proxied=False):
        counter = self.rpc_client.next_counter()
        if proxied:
            msgtype = const.MSG_TYPE_PROXY_PUBLISH_IS_CHUNKED
        else:
            msgtype = const.MSG_TYPE_PUBLISH_IS_CHUNKED
        for target in targets:
            target.push((msgtype,
                (service, routing_id, method, counter, kwargs)))

        for chunk in chunks:
            for target in targets:
                target.push((msgtype + 3, (counter, chunk)))

        for target in targets:
            target.push((msgtype + 6, counter))

    def send_proxied_rpc(
            self, service, routing_id, method, args, kwargs, singular):
        log.debug("sending proxied_rpc %r" % ((service, routing_id, method),))
        return self.rpc_client.request(
                [self.peers.values()[0]],
                (service, routing_id, method, bool(singular), args, kwargs),
                singular)

    def target_selection(self, peers, service, routing_id, method):
        by_addr = {}
        for peer in peers:
            if isinstance(peer, LocalTarget):
                by_addr[None] = peer
            else:
                by_addr[peer.ident] = peer
        choice = hooks._get(self.hooks, 'select_peer')(
                by_addr.keys(), service, routing_id, method)
        return by_addr[choice]

    def send_rpc(self, service, routing_id, method, args, kwargs,
            singular):
        handler, schedule = self.find_local_handler(
                const.MSG_TYPE_RPC_REQUEST, service, routing_id, method)
        routes = []
        if handler is not None:
            routes.append(LocalTarget(self, handler, schedule))

        peers = list(self.find_peer_routes(
            const.MSG_TYPE_RPC_REQUEST, service, routing_id))
        routes.extend(peers)

        if singular and len(peers) > 1:
            routes = [self.target_selection(
                    routes, service, routing_id, method)]
            if not isinstance(routes[0], LocalTarget):
                handler = None

        if handler is not None:
            log.debug("locally handling rpc_request %r %s" %
                    ((service, routing_id, method),
                    "scheduled" if schedule else "immediately"))

        if peers and not (singular and handler):
            log.debug("sending rpc_request %r to %d peers" %
                    ((service, routing_id, method),
                    len(routes) - bool(handler)))

        return self.rpc_client.request(
                routes, (service, routing_id, method, args, kwargs), singular)

    def send_proxied_publish(self, service, routing_id, method, args, kwargs,
            singular=False):
        log.debug("sending proxied_publish %r" %
                ((service, routing_id, method),))
        if len(args) == 1 and hasattr(args[0], "__iter__") \
                and not hasattr(args[0], "__len__"):
            self.send_chunked_publish(service, routing_id, method, args[0],
                    kwargs, [self.peers.values()[0]], proxied=True)
        else:
            self.peers.values()[0].push(
                    (const.MSG_TYPE_PROXY_PUBLISH,
                        (service, routing_id, method, args, kwargs, singular)))

    def publish_handler(self, handler, msg, source, args, kwargs):
        log.debug("executing publish handler for %r from %r" % (msg, source))
        try:
            handler(*args, **kwargs)
        except Exception:
            log.error("exception handling publish %r from %r" % (msg, source))
            greenhouse.handle_exception(*sys.exc_info())

    def rpc_handler(self, peer, counter, handler, args, kwargs,
            proxied=False, scheduled=False):
        req_type = "proxy_request" if proxied else "rpc_request"
        log.debug("executing %s handler for %d from %r" %
                (req_type, counter, peer.ident))

        response = (proxied and const.MSG_TYPE_PROXY_RESPONSE
                or const.MSG_TYPE_RPC_RESPONSE)

        try:
            rc = 0
            result = handler(*args, **kwargs)
        except errors.HandledError, exc:
            log.error("responding with RPC_ERR_KNOWN (%d) to %s %d" %
                    (exc.code, req_type, counter))
            rc = const.RPC_ERR_KNOWN
            result = (exc.code, exc.args)
            greenhouse.handle_exception(*sys.exc_info())
        except Exception:
            log.error("responding with RPC_ERR_UNKNOWN to %s %d" %
                    (req_type, counter))
            rc = const.RPC_ERR_UNKNOWN
            result = traceback.format_exception(*sys.exc_info())
            greenhouse.handle_exception(*sys.exc_info())

        try:
            msg = peer.dump((response, (counter, rc, result)))
        except TypeError:
            log.error("responding with RPC_ERR_UNSER_RESP to %s %d" %
                    (req_type, counter))
            msg = peer.dump((response,
                (counter, const.RPC_ERR_UNSER_RESP, repr(result))))
            greenhouse.handle_exception(*sys.exc_info())
        else:
            log.debug("responding with MSG_TYPE_RESPONSE to %s %d" %
                    (req_type, counter))

        peer.push_string(msg)

    # callback for peer objects to pass up a message
    def incoming(self, peer, msg):
        msg_type, msg = msg
        handler = self.handlers.get(msg_type, None)

        if handler is None:
            # drop unrecognized messages
            log.warn("received unrecognized message type %r from %r" %
                    (msg_type, peer.ident))
            return

        handler(self, peer, msg)

    def incoming_publish(self, peer, msg):
        if not isinstance(msg, tuple) or len(msg) != 5:
            # drop malformed messages
            log.warn("received malformed publish from %r" % (peer.ident,))
            return

        service, routing_id, method, args, kwargs = msg

        handler, schedule = self.find_local_handler(
                const.MSG_TYPE_PUBLISH, service, routing_id, method)
        if handler is None:
            # drop mis-delivered messages
            log.warn("received mis-delivered publish %r from %r" %
                    (msg[:3], peer.ident))
            return

        log.debug("handling publish %r from %r %s" %
                (msg[:3], peer.ident,
                "scheduled" if schedule else "immediately"))

        if schedule:
            greenhouse.schedule(self.publish_handler,
                    args=(handler, msg[:3], peer.ident, args, kwargs))
        else:
            self.publish_handler(handler, msg[:3], peer.ident, args, kwargs)

    def incoming_rpc_request(self, peer, msg):
        if not isinstance(msg, tuple) or len(msg) != 6:
            # drop malformed messages
            log.warn("received malformed rpc_request from %r" % (peer.ident,))
            return

        counter, service, routing_id, method, args, kwargs = msg

        handler, schedule = self.find_local_handler(
                const.MSG_TYPE_RPC_REQUEST, service, routing_id, method)
        if handler is None:
            if any(routing_id & mask == value
                    for mask, value, handlers in self.local_subs.get(
                            (const.MSG_TYPE_RPC_REQUEST, service), [])):
                log.warn("received rpc_request %r for unknown method from %r" %
                        (msg[:4], peer.ident))
                rc = const.RPC_ERR_NOMETHOD
            else:
                log.warn("received mis-delivered rpc_request %r from %r" %
                        (msg[:4], peer.ident))
                rc = const.RPC_ERR_NOHANDLER

            # mis-delivered message
            peer.push((const.MSG_TYPE_RPC_RESPONSE,
                    (counter, rc, None)))
            return

        log.debug("handling rpc_request %r from %r %s" % (
                msg[:4], peer.ident,
                "scheduled" if schedule else "immediately"))

        if schedule:
            greenhouse.schedule(self.rpc_handler,
                    args=(peer, counter, handler, args, kwargs),
                    kwargs={'scheduled': True})
        else:
            self.rpc_handler(peer, counter, handler, args, kwargs)

    def incoming_rpc_response(self, peer, msg):
        if not isinstance(msg, tuple) or len(msg) != 3:
            # drop malformed responses
            log.warn("received malformed rpc_response from %r" % (peer.ident,))
            return

        counter, rc, result = msg

        if counter in self.inflight_proxies:
            log.debug("received a proxied response %r from %r" %
                    (msg[:2], peer.ident))
            self.proxied_response(counter, rc, result)
        elif (counter not in self.rpc_client.inflight or
                peer.ident not in self.rpc_client.inflight[counter]):
            # drop mistaken responses
            log.warn("received mis-delivered rpc_response %d from %r" %
                    (msg[:2], peer.ident))
            return

        log.debug("received rpc_response %r from %r" % (msg[:2], peer.ident))

        self.rpc_client.response(peer, counter, rc, result)

    def proxied_response(self, counter, rc, result):
        entry = self.inflight_proxies[counter]
        entry['awaiting'] -= 1
        if not entry['awaiting']:
            del self.inflight_proxies[counter]

        log.debug("forwarding proxied response to %r, %d remaining" %
                (entry['peer'].ident, entry['awaiting']))

        entry['peer'].push((const.MSG_TYPE_PROXY_RESPONSE,
                (entry['client_counter'], rc, result)))

    def incoming_proxy_publish(self, peer, msg):
        if not isinstance(msg, tuple) or len(msg) != 6:
            # drop malformed messages
            log.warn("received malformed proxy_publish from %r" %
                    (peer.ident,))
            return

        log.debug("forwarding a proxy_publish %r from %r" %
                (msg[:3], peer.ident))

        self.send_publish(*(msg[:5] + (True, msg[5])))

    def incoming_proxy_request(self, peer, msg):
        if not isinstance(msg, tuple) or len(msg) != 7:
            # drop badly formed messages
            log.warn("received malformed proxy_request from %r" %
                    (peer.ident,))
            return
        cli_counter, service, routing_id, method, singular, args, kwargs = msg

        # find local handlers
        handler, schedule = self.find_local_handler(
                const.MSG_TYPE_RPC_REQUEST, service, routing_id, method)

        # find remote targets and count up total handlers
        targets = list(self.find_peer_routes(
                const.MSG_TYPE_RPC_REQUEST, service, routing_id))
        target_count = len(targets) + bool(handler)

        # pick the single target for 'singular' proxy RPCs
        if target_count > 1 and singular:
            target_count = 1
            target = self.target_selection(
                    targets + [LocalTarget(self, handler, schedule)],
                    service, routing_id, method)
            if isinstance(target, LocalTarget):
                targets = []
            else:
                handler = None
                targets = [target]

        # handle it locally if it's aimed at us
        if handler is not None:
            log.debug("locally handling proxy_request %r %s" % (
                    msg[:4], "scheduled" if schedule else "immediately"))
            if schedule:
                greenhouse.schedule(self.rpc_handler,
                        args=(peer, cli_counter, handler, args, kwargs),
                        kwargs={'proxied': True, 'scheduled': True})
            else:
                self.rpc_handler(
                        peer, cli_counter, handler, args, kwargs, True)

        if targets:
            log.debug("forwarding proxy_request %r to %d peers" %
                    (msg[:4], target_count - bool(handler)))

            rpc = self.rpc_client.request(
                    targets, (service, routing_id, method, args, kwargs))

            self.inflight_proxies[rpc.counter] = {
                'awaiting': len(targets),
                'client_counter': cli_counter,
                'peer': peer,
            }

        send_nomethod = False
        if handler is None and not targets and self.locally_handles(
                const.MSG_TYPE_RPC_REQUEST, service, routing_id):
            # if there are no remote handlers and we only fail locally because
            # of the method, send a NOMETHOD error and include ourselves in the
            # target_count so the client can distinguish between "no method"
            # and "unroutable"
            log.warn("received proxy_request %r for unknown method" %
                    (msg[:4],))
            target_count += 1
            send_nomethod = True

        peer.push((const.MSG_TYPE_PROXY_RESPONSE_COUNT,
                (cli_counter, target_count)))

        # must send the response after the response_count
        # or the client gets confused
        if send_nomethod:
            peer.push((const.MSG_TYPE_PROXY_RESPONSE,
                (cli_counter, const.RPC_ERR_NOMETHOD, None)))

    def incoming_proxy_query_count(self, peer, msg):
        if not isinstance(msg, tuple) or len(msg) != 5:
            # drop malformed queries
            log.warn("received malformed proxy_query_count from %r" %
                    (peer.ident,))
            return
        counter, msg_type, service, routing_id, method = msg

        log.debug("received proxy_query_count %r from %r" %
                (msg, peer.ident))

        local, scheduled = self.find_local_handler(
                msg_type, service, routing_id, method)
        target_count = (local is not None) + len(list(
            self.find_peer_routes(msg_type, service, routing_id)))

        log.debug("sending proxy_response %r for query_count %r to %r" %
                ((counter, 0, target_count), msg, peer.ident))

        peer.push((const.MSG_TYPE_PROXY_RESPONSE, (counter, 0, target_count)))

    def incoming_proxy_response(self, peer, msg):
        if not isinstance(msg, tuple) or len(msg) != 3:
            # drop malformed responses
            log.warn("received malformed proxy_response from %r" %
                    (peer.ident,))
            return

        counter, rc, result = msg

        if counter not in self.rpc_client.inflight:
            # drop mistaken responses
            log.warn("received mis-delivered proxy_response %r from %r" %
                    (msg[:2], peer.ident))
            return

        log.debug("received proxy_response %r from %r" %
                (msg[:2], peer.ident))

        self.rpc_client.response(peer, counter, rc, result)

    def incoming_proxy_response_count(self, peer, msg):
        if not isinstance(msg, tuple) or len(msg) != 2:
            # drop malformed responses
            log.warn("received malformed proxy_response_count from %r" %
                    (peer.ident,))
            return
        counter, target_count = msg

        log.debug("received proxy_response_count %r from %r" %
                (msg, peer.ident))

        self.rpc_client.expect(peer, counter, target_count)

    handlers = {
        const.MSG_TYPE_ANNOUNCE: incoming_announce,
        const.MSG_TYPE_UNSUBSCRIBE: incoming_unsubscribe,
        const.MSG_TYPE_PUBLISH: incoming_publish,
        const.MSG_TYPE_RPC_REQUEST: incoming_rpc_request,
        const.MSG_TYPE_RPC_RESPONSE: incoming_rpc_response,
        const.MSG_TYPE_PROXY_PUBLISH: incoming_proxy_publish,
        const.MSG_TYPE_PROXY_REQUEST: incoming_proxy_request,
        const.MSG_TYPE_PROXY_RESPONSE: incoming_proxy_response,
        const.MSG_TYPE_PROXY_RESPONSE_COUNT: incoming_proxy_response_count,
        const.MSG_TYPE_PROXY_QUERY_COUNT: incoming_proxy_query_count,
    }


class LocalTarget(object):
    def __init__(self, dispatcher, handler, schedule):
        self.dispatcher = dispatcher
        self.handler = handler
        self.schedule = schedule
        self.ident = None

    def push(self, msg):
        msgtype, msg = msg
        if msgtype == const.MSG_TYPE_RPC_REQUEST:
            counter, service, routing_id, method, args, kwargs = msg
            if self.schedule:
                greenhouse.schedule(self.dispatcher.rpc_handler,
                        args=(self, counter, self.handler, args, kwargs))
            else:
                self.dispatcher.rpc_handler(
                        self, counter, self.handler, args, kwargs)

        elif msgtype == const.MSG_TYPE_RPC_RESPONSE:
            # sent back here via dispatcher.rpc_handler
            counter, rc, result = msg
            self.dispatcher.rpc_client.response(self, counter, rc, result)

        elif msgtype == const.MSG_TYPE_PUBLISH:
            service, routing_id, method, args, kwargs = msg
            if self.schedule:
                greenhouse.schedule(self.handler, args=args, kwargs=kwargs)
            else:
                try:
                    self.handler(*args, **kwargs)
                except Exception:
                    log.error("exception handling local publish %r" %
                            ((service, routing_id, method),))
                    greenhouse.handle_exception(*sys.exc_info())

    # trick RPCClient.request
    # in the case of a local handler it doesn't have to go over the wire, so
    # there's no issue with unserializable arguments (or return values). so
    # we'll skip the "dump" phase and just "push" the object itself
    push_string = push
    def dump(self, msg):
        return msg
