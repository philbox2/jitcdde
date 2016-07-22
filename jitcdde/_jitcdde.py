#!/usr/bin/python3
# -*- coding: utf-8 -*-

from __future__ import print_function, absolute_import, division

from inspect import isgeneratorfunction
from warnings import warn
import jitcdde._python_core as python_core
import sympy
import numpy as np

def provide_advanced_symbols():
	t = sympy.Symbol("t", real=True)
	current_y = sympy.Function("current_y")
	anchors = sympy.Function("anchors")
	past_y = sympy.Function("past_y")
	
	class y(sympy.Function):
		@classmethod
		def eval(cls, index, time=t):
			if time == t:
				return current_y(index)
			else:
				return past_y(time, index, anchors(time))
	
	return t, y, current_y, past_y, anchors

def provide_basic_symbols():
	return provide_advanced_symbols()[:2]

def _handle_input(f_sym,n):
	if isgeneratorfunction(f_sym):
		n = n or sum(1 for _ in f_sym())
		return ( f_sym, n )
	else:
		len_f = len(f_sym)
		if (n is not None) and(len_f != n):
			raise ValueError("len(f_sym) and n do not match.")
		return (lambda: (entry.doit() for entry in f_sym), len_f)

def depends_on_any(helper, other_helpers):
	for other_helper in other_helpers:
		if helper[1].has(other_helper[0]):
			return True
	return False

def _sort_helpers(helpers):
	if len(helpers)>1:
		for j,helper in enumerate(helpers):
			if not depends_on_any(helper, helpers):
				helpers.insert(0,helpers.pop(j))
				break
		else:
			raise ValueError("Helpers have cyclic dependencies.")
		
		helpers[1:] = _sort_helpers(helpers[1:])
	
	return helpers

def _sympify_helpers(helpers):
	return [(helper[0], sympy.sympify(helper[1]).doit()) for helper in helpers]

class UnsuccessfulIntegration(Exception):
    pass

class jitcdde():
	def __init__(self, f_sym, helpers=None, n=None):
		self.f_sym, self.n = _handle_input(f_sym,n)
		self.f = None
		self.helpers = _sort_helpers(_sympify_helpers(helpers or []))
		self._y = []
		self._tmpdir = None
		self._modulename = "jitced"
		self.past = []
		
	def add_past_point(self, time, state, diff):
		self.past.append((time, state, diff))
	
	def generate_f_lambda(self):
		self.DDE = python_core.dde_integrator(self.f_sym(), self.past, self.helpers)
	
	def set_integration_parameters(self,
			atol = 0.0,
			rtol = 1e-5,
			first_step = 1.0,
			min_step = 1e-10,
			max_step = 10.0,
			decrease_threshold = 1.1,
			increase_threshold = 0.5,
			safety_factor = 0.9,
			max_factor = 5.0,
			min_factor = 0.2,
			pws_atol = 0.0,
			pws_rtol = 1e-5,
			pws_max_iterations = 10,
			pws_adaption_factor = 0.5,
			raise_exception = False,
			):
		
		"""
		TODO: component-wise shit
		"""
		
		assert min_step <= first_step <= max_step, "Bogus step parameters."
		assert decrease_threshold>=1.0, "decrease_threshold smaller than 1"
		assert increase_threshold<=1.0, "increase_threshold larger than 1"
		assert max_factor>=1.0, "max_factor smaller than 1"
		assert min_factor<=1.0, "min_factor larger than 1"
		assert safety_factor<=1.0, "safety_factor larger than 1"
		assert np.all(atol>=0.0), "negative atol"
		assert np.all(rtol>=0.0), "negative rtol"
		if atol==0 and rtol==0:
			warn("atol and rtol are both 0. You probably do not want this.")
		assert np.all(pws_atol>=0.0), "negative pws_atol"
		assert np.all(pws_rtol>=0.0), "negative pws_rtol"
		assert 0<pws_max_iterations, "non-positive pws_max_iterations"
		assert 0.0<pws_adaption_factor<1.0, "bogus pws_adaption_factor"
		
		self.atol = atol
		self.rtol = rtol
		self.dt = first_step
		self.min_step = min_step
		self.max_step = max_step
		self.decrease_threshold = decrease_threshold
		self.increase_threshold = increase_threshold
		self.safety_factor = safety_factor
		self.max_factor = max_factor
		self.min_factor = min_factor
		self.do_raise_exception = raise_exception
		self.pws_atol = pws_atol
		self.pws_rtol = pws_rtol
		self.pws_max_iterations = pws_max_iterations
		self.pws_adaption_factor = pws_adaption_factor
		self.q = 3.
		self.pws_factor = 1.0
	
	def _control_for_min_step(self):
		if self.pws_factor*self.dt < self.min_step:
			raise UnsuccessfulIntegration(
				"Step size under min_step (%f). dt=%f, pws_factor=%f" %
				(self.min_step, self.dt, self.pws_factor)
				)
	
	def _adjust_step_size(self):
		p = np.max(np.abs(self.DDE.error)/(self.atol + self.rtol*np.abs(self.DDE.past[-1][1])))
		
		if p > self.decrease_threshold:
			self.dt *= max(self.safety_factor*p**(-1/self.q), self.min_factor)
			self._control_for_min_step()
		else:
			self.successful = True
			self.DDE.accept_step()
			if p < self.increase_threshold:
				self.dt *= min(self.safety_factor*p**(-1/(self.q+1)), self.max_factor)
	
	def integrate(self, target_time):
		try:
			while self.DDE.t < target_time:
				self.successful = False
				while not self.successful:
					self.DDE.get_next_step(self.pws_factor*self.dt)
					if self.DDE.past_within_step:
						
						# If possible, adjust step size to make integration explicit:
						if self.DDE.past_within_step < self.pws_adaption_factor*self.pws_factor*self.dt:
							self.pws_factor *= self.pws_adaption_factor
							self._control_for_min_step()
							continue
						
						# Try to come within an acceptable error within pws_max_iterations iterations; otherwise adjust step size:
						for count in range(1,self.pws_max_iterations+1):
							old_new_y = self.DDE.past[-1][1]
							self.DDE.get_next_step(self.pws_factor*self.dt)
							new_y = self.DDE.past[-1][1]
							difference = np.abs(new_y-old_new_y)
							tolerance = self.pws_atol + np.abs(self.pws_rtol*new_y)
							if np.all(difference < tolerance):
								if count < 1/self.pws_adaption_factor:
									self.pws_factor = min(
										1.0,
										self.pws_factor/self.pws_adaption_factor
										)
								break
						else:
							self.pws_factor *= self.pws_adaption_factor
							self._control_for_min_step()
							continue
					
					self._adjust_step_size()
		
		except UnsuccessfulIntegration as error:
			self.successful = False
			if self.do_raise_exception:
				raise error
			else:
				warn(str(error))
				return np.nan*np.ones(self.n)
		
		else:
			return self.DDE.get_past_state(
				target_time,
				(self.DDE.past[-2], self.DDE.past[-1])
				)