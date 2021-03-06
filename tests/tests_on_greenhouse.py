#!/usr/bin/env python
# vim: fileencoding=utf8:et:sta:ai:sw=4:ts=4:sts=4

import logging
import traceback
import unittest

import greenhouse
import junction
import junction.errors


TIMEOUT = 0.015
PORT = 7000

GTL = greenhouse.Lock()

#junction.configure_logging(level=1)
#greenhouse.global_exception_handler(traceback.print_exception)

# base class stolen from the greenhouse test suite
class StateClearingTestCase(unittest.TestCase):
    def setUp(self):
        junction.activate_greenhouse()

        GTL.acquire()

        state = greenhouse.scheduler.state
        state.awoken_from_events.clear()
        state.timed_paused.clear()
        state.paused[:] = []
        state.descriptormap.clear()
        state.to_run.clear()

        greenhouse.reset_poller()

    def tearDown(self):
        GTL.release()


class JunctionTests(object):
    def create_hub(self, peers=None):
        global PORT
        peer = junction.Hub(("127.0.0.1", PORT), peers or [])
        PORT += 2
        peer.start()
        return peer

    def setUp(self):
        super(JunctionTests, self).setUp()

        self.peer = self.create_hub()
        self.connection = self.peer

        self.build_sender()

        self._handled_errors_copy = junction.errors.HANDLED_ERROR_TYPES.copy()

    def tearDown(self):
        self.peer.shutdown()
        self.sender.shutdown()
        del self.peer, self.sender

        junction.errors.HANDLED_ERROR_TYPES = self._handled_errors_copy

        super(JunctionTests, self).tearDown()

    def test_publish_success(self):
        results = []
        ev = greenhouse.Event()

        @self.peer.accept_publish("service", 0, 0, "method")
        def handler(item):
            results.append(item)
            if len(results) == 4:
                ev.set()

        for i in xrange(4):
            greenhouse.pause()

        self.sender.publish("service", 0, "method", (1,), {})
        self.sender.publish("service", 0, "method", (2,), {})
        self.sender.publish("service", 0, "method", (3,), {})
        self.sender.publish("service", 0, "method", (4,), {})

        ev.wait(TIMEOUT)

        self.assertEqual(results, [1, 2, 3, 4])

    def test_publish_ruled_out_by_service(self):
        results = []
        ev = greenhouse.Event()

        @self.peer.accept_publish("service1", 0, 0, "method")
        def handler(item):
            results.append(item)
            ev.set()

        for i in xrange(4):
            greenhouse.pause()

        try:
            self.sender.publish("service2", 0, "method", (1,), {})
        except junction.errors.Unroutable:
            # eat this as Clients don't get this raised, only Hubs
            pass

        assert ev.wait(TIMEOUT)

        self.assertEqual(results, [])

    def test_publish_ruled_out_by_method(self):
        results = []
        ev = greenhouse.Event()

        @self.peer.accept_publish("service", 0, 0, "method1")
        def handler(item):
            results.append(item)
            ev.set()

        for i in xrange(4):
            greenhouse.pause()

        try:
            self.sender.publish("service", 0, "method2", (1,), {})
        except junction.errors.Unroutable:
            # eat this as Clients don't get this raised, only Hubs
            pass

        assert ev.wait(TIMEOUT)

        self.assertEqual(results, [])

    def test_publish_ruled_out_by_routing_id(self):
        results = []
        ev = greenhouse.Event()

        # only sign up for even routing ids
        @self.peer.accept_publish("service", 1, 0, "method")
        def handler(item):
            results.append(item)
            ev.set()

        for i in xrange(4):
            greenhouse.pause()

        try:
            self.sender.publish("service", 1, "method", (1,), {})
        except junction.errors.Unroutable:
            # eat this as Clients don't get this raised, only Hubs
            pass

        assert ev.wait(TIMEOUT)

        self.assertEqual(results, [])

    def test_chunked_publish_success(self):
        results = []
        ev = greenhouse.Event()

        @self.peer.accept_publish("service", 0, 0, "method")
        def handler(items):
            for item in items:
                results.append(item)
            ev.set()

        for i in xrange(4):
            greenhouse.pause()

        self.sender.publish("service", 0, "method", ((x for x in xrange(5)),))

        assert not ev.wait(TIMEOUT)

        self.assertEqual(results, [0, 1, 2, 3, 4])

    def test_rpc_success(self):
        handler_results = []
        sender_results = []

        @self.peer.accept_rpc("service", 0, 0, "method")
        def handler(x):
            handler_results.append(x)
            return x ** 2

        for i in xrange(4):
            greenhouse.pause()

        sender_results.append(self.sender.rpc("service", 0, "method", (1,), {},
            timeout=TIMEOUT))
        sender_results.append(self.sender.rpc("service", 0, "method", (2,), {},
            timeout=TIMEOUT))
        sender_results.append(self.sender.rpc("service", 0, "method", (3,), {},
            timeout=TIMEOUT))
        sender_results.append(self.sender.rpc("service", 0, "method", (4,), {},
            timeout=TIMEOUT))

        self.assertEqual(handler_results, [1, 2, 3, 4])
        self.assertEqual(sender_results, [1, 4, 9, 16])

    def test_rpc_ruled_out_by_service(self):
        results = []
        ev = greenhouse.Event()

        @self.peer.accept_rpc("service1", 0, 0, "method")
        def handler(item):
            results.append(item)
            ev.set()

        for i in xrange(4):
            greenhouse.pause()

        self.assertRaises(junction.errors.Unroutable,
                self.sender.rpc, "service2", 0, "method", (1,), {}, TIMEOUT)

        assert ev.wait(TIMEOUT)

        self.assertEqual(results, [])

    def test_rpc_ruled_out_by_method(self):
        results = []

        self.peer.accept_rpc("service", 0, 0, "method1", results.append)

        for i in xrange(4):
            greenhouse.pause()

        self.assertRaises(junction.errors.UnsupportedRemoteMethod,
                self.sender.rpc, "service", 0, "method2", (1,), {}, TIMEOUT)

    def test_rpc_ruled_out_by_routing_id(self):
        results = []

        self.peer.accept_rpc("service", 1, 0, "method", results.append)

        for i in xrange(4):
            greenhouse.pause()

        with self.assertRaises(junction.errors.Unroutable):
            self.sender.rpc("service", 1, "method", (1,), timeout=TIMEOUT)

        self.assertEqual(results, [])

    def test_rpc_handler_recognized_exception(self):
        class CustomError(junction.errors.HandledError):
            code = 3

        def handler():
            raise CustomError("gaah")

        self.peer.accept_rpc("service", 0, 0, "method", handler)

        for i in xrange(4):
            greenhouse.pause()

        try:
            self.sender.rpc('service', 0, 'method', (), {}, TIMEOUT)
        except CustomError, exc:
            result = exc
        else:
            assert 0, "should have raised CustomError"

        self.assertEqual(result.args[0], self.connection.addr)
        self.assertEqual(result.args[1], "gaah")

    def test_rpc_handler_unknown_exception(self):
        class CustomError(Exception):
            pass

        def handler():
            raise CustomError("WOOPS")

        self.peer.accept_rpc("service", 0, 0, "method", handler)

        for i in xrange(4):
            greenhouse.pause()

        try:
            result = self.sender.rpc("service", 0, "method", (), {}, TIMEOUT)
        except junction.errors.RemoteException, exc:
            result = exc
        else:
            assert 0, 'should have raised RemoteException'

        self.assertEqual(result.args[0], self.connection.addr)
        self.assertEqual(result.args[1].splitlines()[-1], "CustomError: WOOPS")

    def test_async_rpc_success(self):
        handler_results = []
        sender_results = []

        def handler(x):
            handler_results.append(x)
            return x ** 2

        self.peer.accept_rpc("service", 0, 0, "method", handler)

        for i in xrange(4):
            greenhouse.pause()

        rpcs = []

        rpcs.append(self.sender.send_rpc("service", 0, "method", (1,), {}))
        rpcs.append(self.sender.send_rpc("service", 0, "method", (2,), {}))
        rpcs.append(self.sender.send_rpc("service", 0, "method", (3,), {}))
        rpcs.append(self.sender.send_rpc("service", 0, "method", (4,), {}))

        while rpcs:
            rpc = junction.wait_any(rpcs, TIMEOUT)
            rpcs.remove(rpc)
            sender_results.append(rpc.value)

        self.assertEqual(rpcs, [])
        self.assertEqual(handler_results, [1, 2, 3, 4])
        self.assertEqual(sender_results, [1, 4, 9, 16])

    def test_singular_rpc(self):
        handler_results = []
        sender_results = []

        @self.peer.accept_rpc("service", 0, 0, "method")
        def handler(x):
            handler_results.append(x)
            return x ** 2

        for i in xrange(4):
            greenhouse.pause()

        sender_results.append(self.sender.rpc("service", 0, "method", (1,), {},
            timeout=TIMEOUT))
        sender_results.append(self.sender.rpc("service", 0, "method", (2,), {},
            timeout=TIMEOUT))
        sender_results.append(self.sender.rpc("service", 0, "method", (3,), {},
            timeout=TIMEOUT))
        sender_results.append(self.sender.rpc("service", 0, "method", (4,), {},
            timeout=TIMEOUT))

        self.assertEqual(handler_results, [1,2,3,4])
        self.assertEqual(sender_results, [1,4,9,16])

    def test_chunked_publish(self):
        results = []

        @self.peer.accept_rpc('service', 0, 0, 'method')
        def handler(chunks):
            for chunk in chunks:
                results.append(chunk)
            return 5

        for i in xrange(4):
            greenhouse.pause()

        def gen():
            yield 1
            yield 2

        self.assertEqual(
                self.sender.rpc('service', 0, 'method', (gen(),),
                    timeout=TIMEOUT),
                5)

        self.assertEqual(results, [1,2])


class HubTests(JunctionTests, StateClearingTestCase):
    def build_sender(self):
        self.sender = junction.Hub(("127.0.0.1", 8000), [self.peer.addr])
        self.sender.start()
        self.sender.wait_connected()

    def test_publish_unroutable(self):
        self.assertRaises(junction.errors.Unroutable,
                self.sender.publish, "service", "method", 0, (), {})

    def test_rpc_receiver_count_includes_self(self):
        @self.peer.accept_rpc('service', 0, 0, 'method')
        def handler():
            return 8

        @self.sender.accept_rpc('service', 0, 0, 'method')
        def handler():
            return 9

        greenhouse.pause_for(TIMEOUT)

        self.assertEqual(2,
                self.sender.rpc_receiver_count('service', 0))

    def test_publish_receiver_count_includes_self(self):
        @self.peer.accept_publish('service', 0, 0, 'method')
        def handler():
            return 8

        @self.sender.accept_publish('service', 0, 0, 'method')
        def handler():
            return 9

        greenhouse.pause_for(TIMEOUT)

        self.assertEqual(2,
                self.sender.publish_receiver_count('service', 0))


class ClientTests(JunctionTests, StateClearingTestCase):
    def build_sender(self):
        self.sender = junction.Client(self.peer.addr)
        self.sender.connect()
        self.sender.wait_connected()


class RelayedClientTests(JunctionTests, StateClearingTestCase):
    def build_sender(self):
        self.relayer = junction.Hub(
                ("127.0.0.1", self.peer.addr[1] + 1), [self.peer.addr])
        self.relayer.start()
        self.connection = self.relayer

        self.sender = junction.Client(self.relayer.addr)
        self.sender.connect()

        self.relayer.wait_connected()
        self.sender.wait_connected()

    def tearDown(self):
        self.relayer.shutdown()
        super(RelayedClientTests, self).tearDown()


class NetworklessDependentTests(StateClearingTestCase):
    def test_some_math(self):
        fut = junction.Future()
        fut.finish(4)
        dep = fut.after(
                lambda x: x * 3).after(
                lambda x: x - 7).after(
                lambda x: x ** 3).after(
                lambda x: x // 2)
        dep.wait(TIMEOUT)
        self.assertEqual(dep.value, 62)


class DownedConnectionTests(StateClearingTestCase):
    def kill_client(self, cli_list):
        cli = cli_list.pop()
        cli._peer.sock.close()

    def kill_hub(self, hub_list):
        hub = hub_list.pop()
        for peer in hub._dispatcher.peers.values():
            peer.sock.close()

    def test_unrelated_rpcs_are_unaffected(self):
        global PORT
        hub = junction.Hub(("127.0.0.1", PORT), [])
        PORT += 2

        @hub.accept_rpc('service', 0, 0, 'method')
        def handle():
            greenhouse.pause_for(TIMEOUT)
            return 1

        hub.start()

        peer = junction.Hub(("127.0.0.1", PORT), [hub.addr])
        PORT += 2
        peer.start()
        peer.wait_connected()

        client = junction.Client(hub.addr)
        client.connect()
        client.wait_connected()
        client = [client]

        greenhouse.schedule(self.kill_client, (client,))

        # hub does a self-rpc during which the client connection goes away
        result = peer.rpc('service', 0, 'method')

        self.assertEqual(result, 1)

    def test_unrelated_self_rpcs_are_unaffected(self):
        global PORT
        hub = junction.Hub(("127.0.0.1", PORT), [])
        PORT += 2

        @hub.accept_rpc('service', 0, 0, 'method')
        def handle():
            greenhouse.pause_for(TIMEOUT)
            return 1

        hub.start()

        client = junction.Client(hub.addr)
        client.connect()
        client.wait_connected()
        client = [client]

        @greenhouse.schedule
        def kill_client():
            # so it'll get GC'd
            cli = client.pop()
            cli._peer.sock.close()

        # hub does a self-rpc during which the client connection goes away
        result = hub.rpc('service', 0, 'method')

        self.assertEqual(result, 1)

    def test_unrelated_client_chunked_publishes_are_unrelated(self):
        global PORT
        hub = junction.Hub(("127.0.0.1", PORT), [])
        PORT += 2

        d = {}

        @hub.accept_publish('service', 0, 0, 'method')
        def handle(x, source):
            for item in x:
                d.setdefault(source, 0)
                d[source] += 1

        hub.start()

        c1 = junction.Client(("127.0.0.1", PORT - 2))
        c1.connect()
        c1.wait_connected()
        c2 = junction.Client(("127.0.0.1", PORT - 2))
        c2.connect()
        c2.wait_connected()

        def gen():
            greenhouse.pause_for(TIMEOUT)
            yield None
            greenhouse.pause_for(TIMEOUT)
            yield None
            greenhouse.pause_for(TIMEOUT)
            yield None

        greenhouse.schedule(c1.publish, args=('service', 0, 'method'),
                kwargs={'args': (gen(),), 'kwargs': {'source': 'a'}})
        greenhouse.schedule(c2.publish, args=('service', 0, 'method'),
                kwargs={'args': (gen(),), 'kwargs': {'source': 'b'}})

        greenhouse.pause_for(TIMEOUT)

        c2 = [c2]
        self.kill_client(c2)

        greenhouse.pause_for(TIMEOUT)
        greenhouse.pause_for(TIMEOUT)
        greenhouse.pause_for(TIMEOUT)

        self.assertEquals(d, {'a': 3, 'b': 1})

    def test_downed_hub_during_chunked_publish_terminates_correctly(self):
        global PORT
        hub = junction.Hub(("127.0.0.1", PORT), [])
        PORT += 2
        l = []
        ev = greenhouse.Event()

        @hub.accept_publish('service', 0, 0, 'method')
        def handle(x):
            for item in x:
                l.append(item)
            ev.set()

        hub.start()

        hub2 = junction.Hub(("127.0.0.1", PORT), [("127.0.0.1", PORT-2)])
        PORT += 2
        hub2.start()
        hub2.wait_connected()
        hub2 = [hub2]

        def gen():
            yield 1
            yield 2
            self.kill_hub(hub2)

        hub2[0].publish('service', 0, 'method', (gen(),))
        ev.wait(TIMEOUT)

        self.assertEqual(l[:2], [1,2])
        self.assertEqual(len(l), 3, l)
        self.assertIsInstance(l[-1], junction.errors.LostConnection)

    def test_downed_hub_during_chunk_pub_to_client_terminates_correctly(self):
        global PORT
        hub = junction.Hub(("127.0.0.1", PORT), [])
        PORT += 2
        l = []
        ev = greenhouse.Event()

        @hub.accept_publish('service', 0, 0, 'method')
        def handle(x):
            for item in x:
                l.append(item)
            ev.set()

        hub.start()

        client = junction.Client(("127.0.0.1", PORT - 2))
        PORT += 2
        client.connect()
        client.wait_connected()
        client = [client]

        def gen():
            yield 1
            yield 2
            self.kill_client(client)

        client[0].publish('service', 0, 'method', (gen(),))
        ev.wait(TIMEOUT)

        self.assertEqual(l[:2], [1,2])
        self.assertEqual(len(l), 3)
        self.assertIsInstance(l[-1], junction.errors.LostConnection)

    def test_downed_recipient_cancels_the_hub_sender_during_chunked_publish(self):
        global PORT
        hub = junction.Hub(("127.0.0.1", PORT), [])
        PORT += 2
        triggered = [False]

        @hub.accept_publish('service', 0, 0, 'method')
        def handle(chunks):
            for item in chunks:
                pass

        hub.start()

        hub2 = junction.Hub(("127.0.0.1", PORT), [("127.0.0.1", PORT - 2)])
        PORT += 2
        hub2.start()
        hub2.wait_connected()

        def gen():
            try:
                while 1:
                    yield None
                    greenhouse.pause_for(TIMEOUT)
            finally:
                triggered[0] = True

        hub2.publish('service', 0, 'method', (gen(),))

        hub = [hub]
        greenhouse.schedule_in(TIMEOUT * 4, self.kill_hub, args=(hub,))

        greenhouse.pause_for(TIMEOUT * 5)

        assert triggered[0]

    def test_downed_recipient_cancels_the_hub_sender_during_chunked_request(self):
        global PORT
        hub = junction.Hub(("127.0.0.1", PORT), [])
        PORT += 2
        triggered = [False]

        @hub.accept_rpc('service', 0, 0, 'method')
        def handle(chunks):
            for item in chunks:
                pass
            return "all done"

        hub.start()

        hub2 = junction.Hub(("127.0.0.1", PORT), [("127.0.0.1", PORT - 2)])
        PORT += 2
        hub2.start()
        hub2.wait_connected()

        def gen():
            try:
                while 1:
                    yield None
                    greenhouse.pause_for(TIMEOUT)
            finally:
                triggered[0] = True

        rpc = hub2.send_rpc('service', 0, 'method', (gen(),))

        hub = [hub]
        greenhouse.schedule_in(TIMEOUT * 4, self.kill_hub, args=(hub,))

        greenhouse.pause_for(TIMEOUT * 5)

        assert triggered[0]


if __name__ == '__main__':
    unittest.main()
