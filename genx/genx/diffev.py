'''
An implementation of the differential evolution algorithm for fitting.
'''
import _thread
import multiprocessing as processing
import pickle
import random as random_mod
import time
from dataclasses import dataclass
from logging import debug
from numpy import *

from .core.config import BaseConfig
from .core.custom_logging import iprint
from .core.Simplex import Simplex
from .exceptions import ErrorBarError
from .model import Model
from .solver_basis import GenxOptimizer, GenxOptimizerCallback, SolverParameterInfo, SolverResultInfo, SolverUpdateInfo


__mpi_loaded__=False
__parallel_loaded__=True
_cpu_count=processing.cpu_count()

try:
    from mpi4py import MPI as mpi
except ImportError:
    rank=0
    __mpi_loaded__=False
    mpi=None
else:
    __mpi_loaded__=True
    comm=mpi.COMM_WORLD
    size=comm.Get_size()
    rank=comm.Get_rank()


class DiffEvDefaultCallbacks(GenxOptimizerCallback):

    def text_output(self, text):
        iprint(text)
        sys.stdout.flush()

    def plot_output(self, update_data):
        pass

    def parameter_output(self, param_info):
        pass

    def fitting_ended(self, result_data):
        pass

    def autosave(self):
        pass


@dataclass
class DiffEvConfig(BaseConfig):
    section='solver'
    km:float=BaseConfig.GParam(0.7, pmin=0.0, pmax=1.0)
    kr:float=BaseConfig.GParam(0.7, pmin=0.0, pmax=1.0)
    allowed_fom_discrepancy:float= 1e-10

    use_pop_mult:bool=True
    pop_size:int=BaseConfig.GParam(50, pmin=5, pmax=10000, label='Fixed size')
    pop_mult:int=BaseConfig.GParam(3, pmin=1, pmax=100, label='Relative size')
    create_trial:str=BaseConfig.GChoice('best_1_bin', ['best_1_bin', 'rand_1_bin', 'best_either_or',
                                                       'rand_either_or', 'jade_best', 'simplex_best_1_bin'],
                                        label='Method')

    use_max_generations:bool=False
    max_generations:int=BaseConfig.GParam(500, pmin=10, pmax=10000, label='Fixed size')
    max_generation_mult:int=BaseConfig.GParam(6, pmin=1, pmax=100, label='Relative size')

    use_start_guess:bool=True
    use_boundaries:bool=True

    max_log_elements:int=BaseConfig.GParam(100000, pmin=1000, pmax=1000000, label=', # elements')
    use_parallel_processing:bool=False
    use_mpi:bool=False
    parallel_processes:int=_cpu_count
    parallel_chunksize:int=10

    use_autosave:bool=False
    autosave_interval:int=BaseConfig.GParam(10, pmin=1, pmax=1000, label=', interval')

    limit_fit_range:bool=False
    fit_xmin:float=BaseConfig.GParam(0.0, pmin=-1000., pmax=1000.)
    fit_xmax:float=BaseConfig.GParam(180.0, pmin=-1000., pmax=1000.)

    save_all_evals:bool=False
    errorbar_level:float=BaseConfig.GParam(1.05, pmin=1.001, pmax=2.0)

    groups={ # for building config dialogs
        'Fitting': [['use_start_guess', 'use_boundaries'], ['use_autosave', 'autosave_interval'],
                    ['save_all_evals', 'max_log_elements']],
        'Differential Evolution':
            ['km', 'kr', 'create_trial',
             ['Population size:', 'use_pop_mult', 'pop_mult', 'pop_size'],
             ['Max. Generations:', 'use_max_generations', 'max_generation_mult', 'max_generations']
             ],
        'Parallel processing': ['use_parallel_processing', 'parallel_processes', 'parallel_chunksize']
        }

class DiffEv(GenxOptimizer):
    '''
    Contains the implementation of the differential evolution algorithm.
    It also contains thread support which is activated by the start_fit 
    function.
    '''
    opt: DiffEvConfig
    model: Model
    fom_log: ndarray
    start_guess: ndarray

    pf=0.5 # probability for mutation
    c=0.07
    simplex_interval = 5  # Interval of running the simplex opt
    simplex_step = 0.05  # first step as a fraction of pop size
    simplex_n = 0.0  # Number of individuals that will be optimized by simplex
    simplex_rel_epsilon = 1000  # The relative epsilon - convergence criteria
    simplex_max_iter = 100  # THe maximum number of simplex runs

    _callbacks: GenxOptimizerCallback=DiffEvDefaultCallbacks()

    parameter_groups=[
        ['Fitting', ['use_start_guess', 'use_boundaries', 'use_autosave', 'autosave_interval']],
        ['Diff. Ev.', ['km', 'kr', 'method']],
        ['Population size', ['use_pop_mult', 'pop_mult', 'pop_size']],
        ['Max. Generatrions', ['use_max_generations', 'max_generations', 'max_generation_mult']],
        ['Parallel processing', ['use_parallel_processing', 'processes', 'chunksize']],
        ]

    def create_mutation_table(self):
        # Mutation schemes implemented
        self.mutation_schemes=[self.best_1_bin, self.rand_1_bin,
                               self.best_either_or, self.rand_either_or,
                               self.jade_best, self.simplex_best_1_bin]

    def __init__(self):
        GenxOptimizer.__init__(self)
        self.create_mutation_table()

        self.model=Model()

        # Definition for the create_trial function
        self.create_trial=self.best_1_bin
        self.update_pop=self.standard_update_pop
        self.init_new_generation=self.standard_init_new_generation

        # Control flags:
        self.running=False  # true if optimization is running
        self.stop=False  # true if the optimization should stop
        self.setup_ok=False  # True if the optimization have been setup
        self.error=None  # None/string if an error ahs occurred

        # Logging variables
        self.fom_log=array([[0, 0]])[0:0]

        self.par_evals=CircBuffer(self.opt.max_log_elements, buffer=array([[]])[0:0])
        self.fom_evals=CircBuffer(self.opt.max_log_elements)

        self.start_guess=array([])


    @property
    def n_fom_evals(self):
        return len(self.fom_evals)

    @property
    def method(self):
        return self.create_trial.__name__

    @method.setter
    def method(self, value):
        names=self.methods
        if value in names:
            self.create_trial=self.mutation_schemes[names.index(value)]
        else:
            raise ValueError("Mutation method has to be in %s"%names)

    @property
    def methods(self):
        return [f.__name__ for f in self.mutation_schemes]

    def write_h5group(self, group):
        """
        Write parameters into hdf5 group
        """
        super().write_h5group(group)

        if not self.opt.save_all_evals:
            group['par_evals']=array([])
            group['fom_evals']=array([])
        else:
            group['par_evals']=self.par_evals.array()
            group['fom_evals']=self.fom_evals.array()

    def read_h5group(self, group):
        """
        Read parameters from a hdf5 group
        """
        self.setup_ok=False
        super().read_h5group(group)

        self.par_evals.copy_from(group['par_evals'][()])
        self.fom_evals.copy_from(group['fom_evals'][()])

    def get_start_guess(self):
        return self.start_guess

    def is_running(self):
        return self.running

    def project_evals(self, index):
        return self.par_evals[:, index], self.fom_evals[:]

    def is_fitted(self):
        return len(self.start_guess)>0

    def is_configured(self):
        return self.setup_ok

    def safe_copy(self, other: 'DiffEv'):
        '''
        Does a safe copy of other to this object. Makes copies of everything
        if necessary. The two objects become decoupled.
        '''
        self.opt=other.opt.copy()

        # True if the optimization have been setup
        self.setup_ok=other.setup_ok

        # Logging variables
        self.fom_log=other.fom_log[:]
        self.par_evals.copy_from(other.par_evals)
        self.fom_evals.copy_from(other.fom_evals)

        if self.setup_ok:
            self.n_pop=other.n_pop
            self.max_gen=other.max_gen

            # Starting values setup
            self.pop_vec=other.pop_vec

            self.start_guess=other.start_guess

            self.trial_vec=other.trial_vec
            self.best_vec=other.best_vec

            self.fom_vec=other.fom_vec
            self.best_fom=other.best_fom
            # Not all implementations have these copied within their files
            # Just ignore if an error occur
            try:
                self.n_dim=other.n_dim
                self.par_min=other.par_min
                self.par_max=other.par_max
            except AttributeError:
                debug("Ignoring undefined parameters from DiffEv object in copy due to error:", exc_info=True)

    def pickle_string(self, clear_evals=False):
        '''
        Saves a copy into a pickled string note that the dynamic
        functions will not be saved. For normal use this is taken care of
        outside this class with the config object.
        '''
        cpy=DiffEv()
        cpy.safe_copy(self)
        if clear_evals:
            cpy.par_evals.buffer=cpy.par_evals.buffer[0:0]
            cpy.fom_evals.buffer=cpy.fom_evals.buffer[0:0]
        cpy.create_trial=None
        cpy.update_pop=None
        cpy.init_new_generation=None

        cpy._callbacks=DiffEvDefaultCallbacks()
        cpy.model=None
        cpy.mutation_schemes=None

        return pickle.dumps(cpy)

    def pickle_load(self, pickled_string):
        '''
        Loads the pickled string into the this object. See pickle_string.
        '''
        obj=pickle.loads(pickled_string.encode('latin1', errors='ignore'))
        obj.create_mutation_table()
        self.safe_copy(obj)

    def reset(self):
        '''
        Resets the optimizer. Note this has to be run if the optimizer is to
        be restarted.
        '''
        self.setup_ok=False

    def connect_model(self, model_obj):
        '''
        Connects the model [model] to this object. Retrives the function
        that sets the variables  and stores a reference to the model.
        '''
        # Retrieve parameters from the model
        (param_funcs, start_guess, par_min, par_max)=model_obj.get_fit_pars()

        # Control parameter setup
        self.par_min=array(par_min)
        self.par_max=array(par_max)
        self.par_funcs=param_funcs
        self.model=model_obj
        self.n_dim=len(param_funcs)
        model_obj.opt.limit_fit_range, model_obj.opt.fit_xmin, model_obj.opt.fit_xmax=(
            self.opt.limit_fit_range,
            self.opt.fit_xmin,
            self.opt.fit_xmax)
        if not self.setup_ok:
            self.start_guess=start_guess

    def init_fitting(self, model_obj):
        '''
        Function to run before a new fit is started with start_fit.
        It initilaize the population and sets the limits on the number
        of generation and the population size.
        '''
        self.connect_model(model_obj)
        if self.opt.use_pop_mult:
            self.n_pop=int(self.opt.pop_mult*self.n_dim)
        else:
            self.n_pop=int(self.opt.pop_size)
        if self.opt.use_max_generations:
            self.max_gen=int(self.opt.max_generations)
        else:
            self.max_gen=int(self.opt.max_generation_mult*self.n_dim*self.n_pop)

        # Starting values setup
        self.pop_vec=[self.par_min+random.rand(self.n_dim)*(self.par_max-self.par_min)
                      for _ in range(self.n_pop)]

        if self.opt.use_start_guess:
            self.pop_vec[0]=array(self.start_guess)

        self.trial_vec=[zeros(self.n_dim) for _ in range(self.n_pop)]
        self.best_vec=self.pop_vec[0]

        self.fom_vec=zeros(self.n_dim)
        self.best_fom=1e20

        # Storage area for JADE archives
        self.km_vec=ones(self.n_dim)*self.opt.km
        self.kr_vec=ones(self.n_dim)*self.opt.kr

        # Logging variables
        self.fom_log=array([[0, 1]])[0:0]
        self.par_evals=CircBuffer(self.opt.max_log_elements, buffer=array([self.par_min])[0:0])
        self.fom_evals=CircBuffer(self.opt.max_log_elements)
        # Number of FOM evaluations
        self.n_fom=0

        if rank==0:
            self.text_output('DE initilized')

        # Remember that everything has been setup ok
        self.setup_ok=True

    def init_fom_eval(self):
        '''
        Makes the eval_fom function
        '''
        # Setting up for parallel processing
        if self.opt.use_parallel_processing and __parallel_loaded__:
            self.text_output('Setting up a pool of workers ...')
            self.setup_parallel()
            self.eval_fom=self.calc_trial_fom_parallel
        elif self.opt.use_mpi and __mpi_loaded__:
            self.setup_parallel_mpi()
            self.eval_fom=self.calc_trial_fom_parallel_mpi
        else:
            self.eval_fom=self.calc_trial_fom

    def start_fit(self, model_obj):
        '''
        Starts fitting in a seperate thred.
        '''
        if not self.running:
            # Initialize the parameters to fit
            self.reset()
            self.init_fitting(model_obj)
            self.init_fom_eval()
            self.stop=False
            # Start fitting in a new thread
            _thread.start_new_thread(self.optimize, ())
            # For debugging
            # self.optimize()
            self.text_output('Starting the fit...')
            # self.running = True
            return True
        else:
            self.text_output('Fit is already running, stop and then start')
            return False

    def stop_fit(self):
        '''
        Stops the fit if it has been started in a seperate theres 
        by start_fit.
        '''
        if self.running:
            self.stop=True
            self.text_output('Trying to stop the fit...')
        else:
            self.text_output('The fit is not running')

    def resume_fit(self, model_obj):
        '''
        Resumes the fitting if has been stopped with stop_fit.
        '''
        if not self.running:
            self.stop=False
            self.connect_model(model_obj)
            self.init_fom_eval()
            n_dim_old=self.n_dim
            if self.n_dim==n_dim_old:
                _thread.start_new_thread(self.optimize, ())
                self.text_output('Restarting the fit...')
                self.running=True
                return True
            else:
                self.text_output('The number of parameters has changed'
                                 ' restart the fit.')
                return False
        else:
            self.text_output('Fit is already running, stop and then start')
            return False

    def optimize(self):
        """Method that does the optimization.

        Note that this method does not run in a separate thread.
        For threading use start_fit, stop_fit and resume_fit instead.
        """
        if self.opt.use_mpi:
            self.optimize_mpi()
        else:
            self.optimize_standard()

    def optimize_standard(self):
        '''
        Method implementing the main loop of the differential evolution
        algorithm. Note that this method does not run in a separate thread.
        For threading use start_fit, stop_fit and resume_fit instead.
        '''

        self.text_output('Calculating start FOM ...')
        self.running=True
        self.error=None
        self.n_fom=0

        self.trial_vec=self.pop_vec[:]
        self.eval_fom()
        [self.par_evals.append(vec, axis=0) for vec in self.pop_vec]
        [self.fom_evals.append(vec) for vec in self.trial_fom]
        self.fom_vec=self.trial_fom[:]

        best_index=argmin(self.fom_vec)
        self.best_vec=copy(self.pop_vec[best_index])
        self.best_fom=self.fom_vec[best_index]
        if len(self.fom_log)==0:
            self.fom_log=r_[self.fom_log, \
                            [[len(self.fom_log), self.best_fom]]]
        # Flag to keep track if there has been any improvements
        # in the fit - used for updates
        self.new_best=True

        self.text_output('Going into optimization ...')

        # Update the plot data for any gui or other output
        self.plot_output()
        self.parameter_output()

        # Just making gen live in this scope as well...
        gen=self.fom_log[-1, 0]
        for gen in range(int(self.fom_log[-1, 0])+1, self.max_gen+int(self.fom_log[-1, 0])+1):
            if self.stop:
                break

            t_start=time.time()

            self.init_new_generation(gen)

            # Create the vectors who will be compared to the 
            # population vectors
            [self.create_trial(index) for index in range(self.n_pop)]
            self.eval_fom()
            # Calculate the fom of the trial vectors and update the population
            [self.update_pop(index) for index in range(self.n_pop)]

            # Add the evaluation to the logging
            [self.par_evals.append(vec, axis=0) for vec in self.trial_vec]
            [self.fom_evals.append(vec) for vec in self.trial_fom]

            # Add the best value to the fom log
            self.fom_log=r_[self.fom_log, [[len(self.fom_log), self.best_fom]]]

            # Let the model calculate the simulation of the best.
            sim_fom=self.calc_sim(self.best_vec)

            # Sanity of the model does the simulations fom agree with
            # the best fom
            if abs(sim_fom-self.best_fom)>self.opt.allowed_fom_discrepancy:
                self.text_output('Disagrement between two different fom'
                                 ' evaluations')
                self.error=('The disagreement between two subsequent '
                            'evaluations is larger than %s. Check the '
                            'model for circular assignments.'
                            %self.opt.allowed_fom_discrepancy)
                break

            # Update the plot data for any gui or other output
            self.plot_output()
            self.parameter_output()

            # Time measurement to track the speed
            t=time.time()-t_start
            if t>0:
                speed=self.n_pop/t
            else:
                speed=999999
            self.text_output('FOM: %.3f Generation: %d Speed: %.1f'% \
                             (self.best_fom, gen, speed))

            self.new_best=False
            # Do an autosave if activated and the interval is correct
            if gen%self.opt.autosave_interval==0 and self.opt.use_autosave:
                self.autosave()

        if not self.error:
            self.text_output('Stopped at Generation: %d after %d fom evaluations...'%(gen, gen*self.n_pop))

        # Lets clean up and delete our pool of workers
        if self.opt.use_parallel_processing:
            self.dismount_parallel()
        self.eval_fom=None

        # Now the optimization has stopped
        self.running=False

        # Run application specific clean-up actions
        self.fitting_ended()

    def optimize_mpi(self):
        '''
        Method implementing the main loop of the differential evolution
        algorithm using mpi. This should only be used from the command line.
        The gui can not handle to use mpi.
        '''

        if rank==0:
            self.text_output('Calculating start FOM ...')
        self.running=True
        self.error=None
        self.n_fom=0
        # Old leftovers before going parallel
        self.fom_vec=[self.calc_fom(vec) for vec in self.pop_vec]
        [self.par_evals.append(vec, axis=0) \
         for vec in self.pop_vec]
        [self.fom_evals.append(vec) for vec in self.fom_vec]

        best_index=argmin(self.fom_vec)
        self.best_vec=copy(self.pop_vec[best_index])
        self.best_fom=self.fom_vec[best_index]
        if len(self.fom_log)==0:
            self.fom_log=r_[self.fom_log, \
                            [[len(self.fom_log), self.best_fom]]]
        # Flag to keep track if there has been any improvements
        # in the fit - used for updates
        self.new_best=True

        if rank==0:
            self.text_output('Going into optimization ...')

        # Update the plot data for any gui or other output
        self.plot_output()
        self.parameter_output()

        # Just making gen live in this scope as well...
        gen=self.fom_log[-1, 0]
        for gen in range(int(self.fom_log[-1, 0])+1, self.max_gen \
                                                     +int(self.fom_log[-1, 0])+1):
            if self.stop:
                break
            t_start=time.time()

            self.init_new_generation(gen)

            # Create the vectors who will be compared to the
            # population vectors
            if rank==0:
                [self.create_trial(index) for index in range(self.n_pop)]
                tmp_trial_vec=self.trial_vec
            else:
                tmp_trial_vec=0
            tmp_trial_vec=comm.bcast(tmp_trial_vec, root=0)
            self.trial_vec=tmp_trial_vec
            self.eval_fom()
            tmp_fom=self.trial_fom
            comm.Barrier()

            # collect forms and reshape them and set the completed tmp_fom to trial_fom
            tmp_fom=comm.gather(tmp_fom, root=0)
            if rank==0:
                tmp_fom_list=[]
                for i in list(tmp_fom):
                    tmp_fom_list=tmp_fom_list+i
                tmp_fom=tmp_fom_list

            tmp_fom=comm.bcast(tmp_fom, root=0)
            self.trial_fom=array(tmp_fom).reshape(self.n_pop, )

            [self.update_pop(index) for index in range(self.n_pop)]

            # Calculate the fom of the trial vectors and update the population
            if rank==0:
                # Add the evaluation to the logging
                [self.par_evals.append(vec, axis=0) for vec in self.trial_vec]
                [self.fom_evals.append(vec) for vec in self.trial_fom]

                # Add the best value to the fom log
                self.fom_log=r_[self.fom_log, [[len(self.fom_log), self.best_fom]]]

                # Let the model calculate the simulation of the best.
                sim_fom=self.calc_sim(self.best_vec)

                # Sanity of the model does the simulations fom agree with
                # the best fom
                if abs(sim_fom-self.best_fom)>self.opt.allowed_fom_discrepancy and rank==0:
                    self.text_output('Disagrement between two different fom'
                                     ' evaluations')
                    self.error=('The disagreement between two subsequent '
                                'evaluations is larger than %s. Check the '
                                'model for circular assignments.'
                                %self.opt.allowed_fom_discrepancy)
                    break

                # Update the plot data for any gui or other output
                self.plot_output()
                self.parameter_output()

                # Let the optimization sleep for a while
                # time.sleep(self.opt.sleep_time)

                # Time measurement to track the speed
                t=time.time()-t_start
                if t>0:
                    speed=self.n_pop/t
                else:
                    speed=999999
                if rank==0:
                    self.text_output('FOM: %.3f Generation: %d Speed: %.1f'%
                                     (self.best_fom, gen, speed))

                self.new_best=False
                # Do an autosave if activated and the interval is correct
                if gen%self.opt.autosave_interval==0 and self.opt.use_autosave:
                    self.autosave()

        if rank==0:
            if not self.error:
                self.text_output('Stopped at Generation: %d after %d fom evaluations...'%(gen, gen*self.n_pop))

        # Lets clean up and delete our pool of workers

        self.eval_fom=None

        # Now the optimization has stopped
        self.running=False

        # Run application specific clean-up actions
        self.fitting_ended()

    def calc_fom(self, vec):
        '''
        Function to calcuate the figure of merit for parameter vector 
        vec.
        '''
        model_obj=self.model
        model_obj.opt.limit_fit_range, model_obj.opt.fit_xmin, model_obj.opt.fit_xmax=(
            self.opt.limit_fit_range,
            self.opt.fit_xmin,
            self.opt.fit_xmax)

        # Set the parameter values
        list(map(lambda func, value: func(value), self.par_funcs, vec))
        fom=self.model.evaluate_fit_func()
        self.n_fom+=1
        return fom

    def calc_trial_fom(self):
        '''
        Function to calculate the fom values for the trial vectors
        '''
        model_obj=self.model
        model_obj.opt.limit_fit_range, model_obj.opt.fit_xmin, model_obj.opt.fit_xmax=(
            self.opt.limit_fit_range,
            self.opt.fit_xmin,
            self.opt.fit_xmax)

        self.trial_fom=[self.calc_fom(vec) for vec in self.trial_vec]

    def calc_sim(self, vec):
        ''' calc_sim(self, vec) --> None
        Function that will evaluate the the data points for
        parameters in vec.
        '''
        model_obj=self.model
        model_obj.opt.limit_fit_range, model_obj.opt.fit_xmin, model_obj.opt.fit_xmax=(
            self.opt.limit_fit_range,
            self.opt.fit_xmin,
            self.opt.fit_xmax)
        # Set the parameter values
        list(map(lambda func, value: func(value), self.par_funcs, vec))

        self.model.evaluate_sim_func()
        return self.model.fom

    def setup_parallel(self):
        '''setup_parallel(self) --> None
        
        setup for parallel proccesing. Creates a pool of workers with
        as many cpus there is available
        '''
        # check if CUDA has been activated
        from .models.lib import paratt, USE_NUMBA
        use_cuda=paratt.Refl.__module__.rsplit('.',1)[1]=='paratt_cuda'
        # reduce numba thread count for numba functions
        if USE_NUMBA:
            numba_procs=max(1, _cpu_count//self.opt.parallel_processes)
        else:
            numba_procs=None
        self.text_output("Starting a pool with %i workers ..."%(self.opt.parallel_processes,))
        self.pool=processing.Pool(processes=self.opt.parallel_processes,
                                  initializer=parallel_init,
                                  initargs=(self.model.pickable_copy(), numba_procs))
        if use_cuda:
            self.pool.apply_async(init_cuda)
        time.sleep(1.0)
        # print "Starting a pool with ", self.opt.parallel_processes, " workers ..."

    def setup_parallel_mpi(self):
        """Inits the number or process used for mpi.
        """

        if rank==0:
            self.text_output("Inits mpi with %i processes ..."%(size,))
        parallel_init(self.model.pickable_copy(), use_cuda=False)
        time.sleep(0.1)

    def dismount_parallel(self):
        ''' dismount_parallel(self) --> None
        Used to close the pool and all its processes
        '''
        self.pool.close()
        self.pool.join()

        # del self.pool

    def calc_trial_fom_parallel(self):
        '''calc_trial_fom_parallel(self) --> None
        
        Function to calculate the fom in parallel using the pool
        '''
        model_obj=self.model
        model_obj.opt.limit_fit_range, model_obj.opt.fit_xmin, model_obj.opt.fit_xmax=(
            self.opt.limit_fit_range,
            self.opt.fit_xmin,
            self.opt.fit_xmax)

        self.trial_fom=self.pool.map(parallel_calc_fom, self.trial_vec, chunksize=self.opt.parallel_chunksize)

    def calc_trial_fom_parallel_mpi(self):
        """ Function to calculate the fom in parallel using mpi
        """
        model_obj=self.model
        model_obj.opt.limit_fit_range, model_obj.opt.fit_xmin, model_obj.opt.fit_xmax=(
            self.opt.limit_fit_range,
            self.opt.fit_xmin,
            self.opt.fit_xmax)

        step_len=int(len(self.trial_vec)/size)
        remain=int(len(self.trial_vec)%size)
        left, right=0, 0

        if rank<=remain-1:
            left=rank*(step_len+1)
            right=(rank+1)*(step_len+1)-1
        elif rank>remain-1:
            left= remain*(step_len+1)+(rank-remain)*step_len
            right= remain*(step_len+1)+(rank-remain+1)*step_len-1
        fom_temp=[]

        for i in range(left, right+1):
            fom_temp.append(parallel_calc_fom(self.trial_vec[i]))

        self.trial_fom=fom_temp

    # noinspection PyArgumentList
    def calc_error_bar(self, index):
        '''
        Calculates the errorbar for one parameter number index. 
        returns a float tuple with the error bars. fom_level is the 
        level which is the upperboundary of the fom is allowed for the
        calculated error.
        '''
        fom_level=self.opt.errorbar_level
        if self.setup_ok:  # and len(self.par_evals) != 0:
            par_values=self.par_evals[:, index]
            values_under_level=compress(self.fom_evals[:]<fom_level*self.best_fom, par_values)
            error_bar_low=values_under_level.min()-self.best_vec[index]
            error_bar_high=values_under_level.max()-self.best_vec[index]
            return error_bar_low, error_bar_high
        else:
            raise ErrorBarError()

    def init_new_generation(self, gen):
        ''' Function that is called every time a new generation starts'''
        pass

    def standard_init_new_generation(self, gen):
        ''' Function that is called every time a new generation starts'''
        pass

    def standard_update_pop(self, index):
        '''
        Function to update population vector index. calcs the figure of merit
        and compares it to the current population vector and also checks
        if it is better than the current best.
        '''
        # fom = self.calc_fom(self.trial_vec[index])
        fom=self.trial_fom[index]
        if fom<self.fom_vec[index]:
            self.pop_vec[index]=self.trial_vec[index].copy()
            self.fom_vec[index]=fom
            if fom<self.best_fom:
                self.new_best=True
                self.best_vec=self.trial_vec[index].copy()
                self.best_fom=fom

    # noinspection PyArgumentList
    def simplex_old_init_new_generation(self, gen):
        '''It will run the simplex method every simplex_interval
             generation with a fracitonal step given by simple_step 
             on the best indivual as well a random fraction of simplex_n individuals.
        '''
        iprint('Inits new generation')
        if gen%self.simplex_interval==0:
            spread=array(self.trial_vec).max(0)-array(self.trial_vec).min(0)
            simp=Simplex(self.calc_fom, self.best_vec, spread*self.simplex_step)
            iprint('Starting simplex run for best vec')
            new_vec, err, _iter=simp.minimize(epsilon=self.best_fom/self.simplex_rel_epsilon,
                                              maxiters=self.simplex_max_iter)
            iprint('FOM improvement: ', self.best_fom-err)

            if self.opt.use_boundaries:
                # Check so that the parameters lie inside the bounds
                ok=bitwise_and(self.par_max>new_vec, self.par_min<new_vec)
                # If not inside make a random re-initialization of that parameter
                new_vec=where(ok, new_vec, random.rand(self.n_dim)* \
                              (self.par_max-self.par_min)+self.par_min)

            new_fom=self.calc_fom(new_vec)
            if new_fom<self.best_fom:
                self.best_fom=new_fom
                self.best_vec=new_vec
                self.pop_vec[0]=new_vec
                self.fom_vec[0]=self.best_fom
                self.new_best=True

            # Apply the simplex to a simplex_n members (0-1)
            for index1 in random_mod.sample(range(len(self.pop_vec)),
                                            int(len(self.pop_vec)*self.simplex_n)):
                iprint('Starting simplex run for member: ', index1)
                mem=self.pop_vec[index1]
                mem_fom=self.fom_vec[index1]
                simp=Simplex(self.calc_fom, mem, spread*self.simplex_step)
                new_vec, err, _iter=simp.minimize(epsilon=self.best_fom/self.simplex_rel_epsilon,
                                                  maxiters=self.simplex_max_iter)
                if self.opt.use_boundaries:
                    # Check so that the parameters lie inside the bounds
                    ok=bitwise_and(self.par_max>new_vec, self.par_min<new_vec)
                    # If not inside make a random re-initialization of that parameter
                    new_vec=where(ok, new_vec, random.rand(self.n_dim)* \
                                  (self.par_max-self.par_min)+self.par_min)

                new_fom=self.calc_fom(new_vec)
                if new_fom<mem_fom:
                    self.pop_vec[index1]=new_vec
                    self.fom_vec[index1]=new_fom
                    if new_fom<self.best_fom:
                        self.best_fom=new_fom
                        self.best_vec=new_vec
                        self.new_best=True

    def simplex_init_new_generation(self, gen:int):
        '''
        It will run the simplex method every simplex_interval
        generation with a fracitonal step given by simple_step
        on the simplex_n*n_pop best individuals.
        '''
        iprint('Inits new generation')
        if gen%self.simplex_interval==0:
            # noinspection PyArgumentList
            spread=array(self.trial_vec).max(axis=0)-array(self.trial_vec).min(axis=0)

            idxs=argsort(self.fom_vec)
            n_ind=int(self.n_pop*self.simplex_n)
            if n_ind==0:
                n_ind=1
            # Apply the simplex to a simplex_n members (0-1)
            for index1 in idxs[:n_ind]:
                self.text_output('Starting simplex run for member: %d'%index1)
                mem=self.pop_vec[index1].copy()
                mem_fom=self.fom_vec[index1]
                simp=Simplex(self.calc_fom, mem, spread*self.simplex_step)
                new_vec, err, _iter=simp.minimize(epsilon=self.best_fom/self.simplex_rel_epsilon,
                                                  maxiters=self.simplex_max_iter)
                if self.opt.use_boundaries:
                    # Check so that the parameters lie inside the bounds
                    ok=bitwise_and(self.par_max>new_vec, self.par_min<new_vec)
                    # If not inside make a random re-initialization of that parameter
                    new_vec=where(ok, new_vec, random.rand(self.n_dim)* \
                                  (self.par_max-self.par_min)+self.par_min)

                new_fom=self.calc_fom(new_vec)
                if new_fom<mem_fom:
                    self.pop_vec[index1]=new_vec.copy()
                    self.fom_vec[index1]=new_fom
                    if new_fom<self.best_fom:
                        self.best_fom=new_fom
                        self.best_vec=new_vec.copy()
                        self.new_best=True

    def simplex_best_1_bin(self, index):
        return self.best_1_bin(index)

    def jade_update_pop(self, index):
        '''
        A modified update pop to handle the JADE variation of Differential evolution
        '''
        fom=self.trial_fom[index]
        if fom<self.fom_vec[index]:
            self.pop_vec[index]=self.trial_vec[index].copy()
            self.fom_vec[index]=fom
            self.updated_kr.append(self.kr_vec[index])
            self.updated_km.append(self.km_vec[index])
            if fom<self.best_fom:
                self.new_best=True
                self.best_vec=self.trial_vec[index].copy()
                self.best_fom=fom

    def jade_init_new_generation(self, gen):
        '''
        A modified generation update for jade
        '''
        if gen>1:
            updated_kms=array(self.updated_km)
            updated_krs=array(self.updated_kr)
            if len(updated_kms)!=0:
                self.opt.km=(1.0-self.c)*self.opt.km+self.c*sum(updated_kms**2)/sum(updated_kms)
                self.opt.kr=(1.0-self.c)*self.opt.kr+self.c*mean(updated_krs)
        self.km_vec=abs(self.opt.km+random.standard_cauchy(self.n_pop)*0.1)
        self.kr_vec=self.opt.kr+random.normal(size=self.n_pop)*0.1
        iprint('km: ', self.opt.km, ', kr: ', self.opt.kr)
        self.km_vec=where(self.km_vec>0, self.km_vec, 0)
        self.km_vec=where(self.km_vec<1, self.km_vec, 1)
        self.kr_vec=where(self.kr_vec>0, self.kr_vec, 0)
        self.kr_vec=where(self.kr_vec<1, self.kr_vec, 1)

        self.updated_kr=[]
        self.updated_km=[]

    def jade_best(self, index):
        vec=self.pop_vec[index]
        # Create mutation vector
        # Select two random vectors for the mutation
        index1=int(random.rand(1)*self.n_pop)
        index2=int(random.rand(1)*len(self.par_evals))
        # Make sure it is not the same vector 
        # while index2 == index1:
        #    index2 = int(random.rand(1)*self.n_pop)

        # Calculate the mutation vector according to the best/1 scheme
        mut_vec=vec+self.km_vec[index]*(self.best_vec-vec)+self.km_vec[index]*(
                self.pop_vec[index1]-self.par_evals[index2])

        # Binomial test to determine which parameters to change
        # given by the recombination constant kr
        recombine=random.rand(self.n_dim)<self.kr_vec[index]
        # Make sure at least one parameter is changed
        recombine[int(random.rand(1)*self.n_dim)]=1
        # Make the recombination
        trial=where(recombine, mut_vec, vec)

        # Implementation of constrained optimization
        if self.opt.use_boundaries:
            # Check so that the parameters lie inside the bounds
            ok=bitwise_and(self.par_max>trial, self.par_min<trial)
            # If not inside make a random re-initialization of that parameter
            trial=where(ok, trial, random.rand(self.n_dim)* \
                        (self.par_max-self.par_min)+self.par_min)
        self.trial_vec[index]=trial
        # return trial

    def best_1_bin(self, index):
        '''
        The default create_trial function for this class. 
        uses the best1bin method to create a new vector from the population.
        '''
        vec=self.pop_vec[index]
        # Create mutation vector
        # Select two random vectors for the mutation
        index1=int(random.rand(1)*self.n_pop)
        index2=int(random.rand(1)*self.n_pop)
        # Make sure it is not the same vector 
        while index2==index1:
            index2=int(random.rand(1)*self.n_pop)

        # Calculate the mutation vector according to the best/1 scheme
        mut_vec=self.best_vec+self.opt.km*(
                self.pop_vec[index1]-self.pop_vec[index2])

        # Binomial test to determine which parameters to change
        # given by the recombination constant kr
        recombine=random.rand(self.n_dim)<self.opt.kr
        # Make sure at least one parameter is changed
        recombine[int(random.rand(1)*self.n_dim)]=1
        # Make the recombination
        trial=where(recombine, mut_vec, vec)

        # Implementation of constrained optimization
        if self.opt.use_boundaries:
            # Check so that the parameters lie inside the bounds
            ok=bitwise_and(self.par_max>trial, self.par_min<trial)
            # If not inside make a random re-initialization of that parameter
            trial=where(ok, trial, random.rand(self.n_dim)* \
                        (self.par_max-self.par_min)+self.par_min)

        self.trial_vec[index]=trial
        # return trial

    def best_either_or(self, index):
        '''
        The either/or scheme for creating a trial. Using the best vector
        as base vector.
        '''
        vec=self.pop_vec[index]
        # Create mutation vector
        # Select two random vectors for the mutation
        index1=int(random.rand(1)*self.n_pop)
        index2=int(random.rand(1)*self.n_pop)
        # Make sure it is not the same vector 
        while index2==index1:
            index2=int(random.rand(1)*self.n_pop)

        if random.rand(1)<self.pf:
            # Calculate the mutation vector according to the best/1 scheme
            trial=self.best_vec+self.opt.km*(
                    self.pop_vec[index1]-self.pop_vec[index2])
        else:
            # Trying something else out more like normal recombination
            trial=vec+self.opt.kr*(
                    self.pop_vec[index1]+self.pop_vec[index2]-2*vec)

        # Implementation of constrained optimization
        if self.opt.use_boundaries:
            # Check so that the parameters lie inside the bounds
            ok=bitwise_and(self.par_max>trial, self.par_min<trial)
            # If not inside make a random re-initialization of that parameter
            trial=where(ok, trial, random.rand(self.n_dim)* \
                        (self.par_max-self.par_min)+self.par_min)
        self.trial_vec[index]=trial
        # return trial

    def rand_1_bin(self, index):
        '''
        The default create_trial function for this class. 
        uses the best1bin method to create a new vector from the population.
        '''
        vec=self.pop_vec[index]
        # Create mutation vector
        # Select three random vectors for the mutation
        index1=int(random.rand(1)*self.n_pop)
        index2=int(random.rand(1)*self.n_pop)
        # Make sure it is not the same vector 
        while index2==index1:
            index2=int(random.rand(1)*self.n_pop)
        index3=int(random.rand(1)*self.n_pop)
        while index3==index1 or index3==index2:
            index3=int(random.rand(1)*self.n_pop)

        # Calculate the mutation vector according to the rand/1 scheme
        mut_vec=self.pop_vec[index3]+self.opt.km*(
                self.pop_vec[index1]-self.pop_vec[index2])

        # Binomial test to determine which parameters to change
        # given by the recombination constant kr
        recombine=random.rand(self.n_dim)<self.opt.kr
        # Make sure at least one parameter is changed
        recombine[int(random.rand(1)*self.n_dim)]=1
        # Make the recombination
        trial=where(recombine, mut_vec, vec)

        # Implementation of constrained optimization
        if self.opt.use_boundaries:
            # Check so that the parameters lie inside the bounds
            ok=bitwise_and(self.par_max>trial, self.par_min<trial)
            # If not inside make a random re-initialization of that parameter
            trial=where(ok, trial, random.rand(self.n_dim)* \
                        (self.par_max-self.par_min)+self.par_min)
        self.trial_vec[index]=trial
        # return trial

    def rand_either_or(self, index):
        '''
        random base vector either/or trial scheme
        '''
        # Create mutation vector
        # Select two random vectors for the mutation
        index1=int(random.rand(1)*self.n_pop)
        index2=int(random.rand(1)*self.n_pop)
        # Make sure it is not the same vector 
        while index2==index1:
            index2=int(random.rand(1)*self.n_pop)
        index0=int(random.rand(1)*self.n_pop)
        while index0==index1 or index0==index2:
            index0=int(random.rand(1)*self.n_pop)

        if random.rand(1)<self.pf:
            # Calculate the mutation vector according to the best/1 scheme
            trial=self.pop_vec[index0]+self.opt.km*(
                    self.pop_vec[index1]-self.pop_vec[index2])
        else:
            # Calculate a continuous recombination
            # Trying something else out more like normal recombination
            trial=self.pop_vec[index0]+self.opt.kr*(
                    self.pop_vec[index1]+self.pop_vec[index2]-2*self.pop_vec[index0])

        # Implementation of constrained optimization
        if self.opt.use_boundaries:
            # Check so that the parameters lie inside the bounds
            ok=bitwise_and(self.par_max>trial, self.par_min<trial)
            # If not inside make a random re-initialization of that parameter
            trial=where(ok, trial, random.rand(self.n_dim)* \
                        (self.par_max-self.par_min)+self.par_min)
        self.trial_vec[index]=trial
        # return trial

    # Different function for accessing and setting parameters that
    # the user should have control over.
    def plot_output(self):
        data=SolverUpdateInfo(
            fom_value=self.model.fom,
            fom_name=self.model.fom_func.__name__,
            fom_log=self.get_fom_log(),
            new_best=self.new_best,
            data=self.model.data
            )
        self._callbacks.plot_output(data)

    def text_output(self, text: str):
        self._callbacks.text_output(text)

    def parameter_output(self):
        param_info=SolverParameterInfo(values=self.best_vec.copy(),
                                       new_best=self.new_best,
                                       population=[vec.copy() for vec in self.pop_vec],
                                       max_val=self.par_max.copy(),
                                       min_val=self.par_min.copy(),
                                       fitting=True)
        self._callbacks.parameter_output(param_info)

    def autosave(self):
        self._callbacks.autosave()

    def fitting_ended(self):
        result = self.get_result_info()
        self._callbacks.fitting_ended(result)

    def get_result_info(self):
        result = SolverResultInfo(
            start_guess=self.start_guess.copy(),
            error_message=self.error,
            values=self.best_vec.copy(),
            new_best=self.new_best,
            population=[vec.copy() for vec in self.pop_vec],
            max_val=self.par_max.copy(),
            min_val=self.par_min.copy(),
            fitting=True
            )
        return result

    def set_callbacks(self, callbacks: GenxOptimizerCallback):
        self._callbacks=callbacks

    # Some get functions

    def get_model(self):
        '''
        Getter that returns the model in use in solver.
        '''
        return self.model

    def get_fom_log(self):
        '''
        Returns the fom as a fcn of iteration in an array. 
        Last element last fom value
        '''
        return array(self.fom_log)

    def get_create_trial(self, index=False):
        '''
        returns the current create trial function name if index is False as
        a string or as index in the mutation_schemes list.
        '''
        pos=self.mutation_schemes.index(self.create_trial)
        if index:
            # return the position
            return pos
        else:
            # return the name
            return self.mutation_schemes[pos].__name__

    def set_km(self, val):
        self.opt.km=val

    def set_kr(self, val):
        self.opt.kr=val

    def set_create_trial(self, val):
        '''
        Raises LookupError if the value val [string] does not correspond
        to a mutation scheme/trial function
        '''
        # Get the names of the available functions
        names=[f.__name__ for f in self.mutation_schemes]
        # Find the position of val

        pos=names.index(val)
        self.create_trial=self.mutation_schemes[pos]
        self.opt.create_trial=val
        if val=='jade_best':
            self.update_pop=self.jade_update_pop
            self.init_new_generation=self.jade_init_new_generation
        elif val=='simplex_best_1_bin':
            self.init_new_generation=self.simplex_init_new_generation
            self.update_pop=self.standard_update_pop
        else:
            self.init_new_generation=self.standard_init_new_generation
            self.update_pop=self.standard_update_pop

    def set_pop_mult(self, val):
        self.opt.pop_mult=val

    def set_pop_size(self, val):
        self.opt.pop_size=int(val)

    def set_max_generations(self, val):
        self.opt.max_generations=int(val)

    def set_max_generation_mult(self, val):
        self.opt.max_generation_mult=val

    def set_sleep_time(self, val):
        self.opt.sleep_time=val

    def set_max_log(self, val):
        self.opt.max_log_elements=val

    def set_use_pop_mult(self, val):
        self.opt.use_pop_mult=val

    def set_use_max_generations(self, val):
        self.opt.use_max_generations=val

    def set_use_start_guess(self, val):
        self.opt.use_start_guess=val

    def set_use_boundaries(self, val):
        self.opt.use_boundaries=val

    def set_use_autosave(self, val):
        self.opt.use_autosave=val

    def set_autosave_interval(self, val):
        self.opt.autosave_interval=int(val)

    def set_use_parallel_processing(self, val):
        if __parallel_loaded__:
            self.opt.use_parallel_processing=val
            self.opt.use_mpi=False if val else self.opt.use_mpi
        else:
            self.opt.use_parallel_processing=False

    def set_use_mpi(self, val):
        """Sets if mpi should use for parallel optimization"""
        if __mpi_loaded__:
            self.opt.use_mpi=val
            self.opt.use_parallel_processing=False if val else self.opt.use_parallel_processing
        else:
            self.opt.use_mpi=False

    def set_processes(self, val):
        self.opt.parallel_processes=int(val)

    def set_chunksize(self, val):
        self.opt.parallel_chunksize=int(val)

    def set_fom_allowed_dis(self, val):
        self.opt.allowed_fom_discrepancy=float(val)

    def __repr__(self):
        output="Differential Evolution Optimizer:\n"
        for gname, group in self.parameter_groups:
            output+='    %s:\n'%gname
            for attr in group:
                output+='        %-30s %s\n'%(attr, getattr(self.opt, attr))
        return output

    @property
    def widget(self):
        return self._repr_ipyw_()

    def _repr_ipyw_(self):
        import ipywidgets as ipw
        entries=[]
        for gname, group in self.parameter_groups:
            gentries=[ipw.HTML("<b>%s:</b>"%gname)]
            for attr in group:
                val=eval('self.opt.%s'%attr, globals(), locals())
                if type(val) is bool:
                    item=ipw.Checkbox(value=val, indent=False, description=attr, layout=ipw.Layout(width='24ex'))
                    entry=item
                elif type(val) is int:
                    entry=ipw.IntText(value=val, layout=ipw.Layout(width='18ex'))
                    item=ipw.VBox([ipw.Label(attr), entry])
                elif type(val) is float:
                    entry=ipw.FloatText(value=val, layout=ipw.Layout(width='18ex'))
                    item=ipw.VBox([ipw.Label(attr), entry])
                elif attr=='method':
                    entry=ipw.Dropdown(value=val, options=self.methods, layout=ipw.Layout(width='18ex'))
                    item=ipw.VBox([ipw.Label(attr), entry])
                else:
                    entry=ipw.Text(value=val, layout=ipw.Layout(width='14ex'))
                    item=ipw.VBox([ipw.Label(attr), entry])
                entry.change_item=attr
                entry.observe(self._ipyw_change, names='value')
                gentries.append(item)
            entries.append(ipw.VBox(gentries, layout=ipw.Layout(width='26ex')))
        return ipw.VBox([ipw.HTML("<h3>Optimizer Settings:</h3>"), ipw.HBox(entries)])

    @staticmethod
    def _ipyw_change(change):
        exec('self.%s=change.new'%change.owner.change_item)

# ==============================================================================
# Functions that is needed for parallel processing!
model=Model(); par_funcs=() # global variables set in functions below

def parallel_init(model_copy: Model, numba_procs=None):
    '''
    parallel initialization of a pool of processes. The function takes a
    pickle safe copy of the model and resets the script module and the compiles
    the script and creates function to set the variables.
    '''
    if numba_procs is not None:
        import numba
        iprint(f"Setting numba threads to {numba_procs}")
        numba.set_num_threads(numba_procs)
    global model, par_funcs
    model=model_copy
    model.reset()
    model.simulate()
    (par_funcs, start_guess, par_min, par_max)=model.get_fit_pars()

def init_cuda():
    iprint("Init CUDA in one worker")
    # activate cuda in subprocesses
    from .models.lib import paratt_cuda
    from .models.lib import neutron_cuda
    from .models.lib import paratt, neutron_refl
    paratt.Refl=paratt_cuda.Refl
    paratt.ReflQ=paratt_cuda.ReflQ
    paratt.Refl_nvary2=paratt_cuda.Refl_nvary2
    neutron_refl.Refl=neutron_cuda.Refl
    from .models.lib import paratt, neutron_refl
    paratt.Refl=paratt_cuda.Refl
    paratt.ReflQ=paratt_cuda.ReflQ
    paratt.Refl_nvary2=paratt_cuda.Refl_nvary2
    neutron_refl.Refl=neutron_cuda.Refl
    iprint("CUDA init done, go to work")


def parallel_calc_fom(vec):
    '''
    function that is used to calculate the fom in a parallel process.
    It is a copy of calc_fom in the DiffEv class
    '''
    global model, par_funcs
    # set the parameter values in the model
    list(map(lambda func, value: func(value), par_funcs, vec))
    # evaluate the model and calculate the fom
    fom=model.evaluate_fit_func()

    return fom

def _calc_fom(model_obj: Model, vec, param_funcs):
    '''
    Function to calcuate the figure of merit for parameter vector
    vec.
    '''
    # Set the parameter values
    list(map(lambda func, value: func(value), param_funcs, vec))

    return model_obj.evaluate_fit_func()

class CircBuffer:
    '''A buffer with a fixed length to store the logging data from the diffev 
    class. Initilized to a maximumlength after which it starts to overwrite
    the data again.
    '''

    def __init__(self, maxlen, buffer=None):
        '''Inits the class with a certain maximum length maxlen.
        '''
        self.maxlen=int(maxlen)
        self.pos=-1
        self.filled=False
        if buffer is None:
            self.buffer=zeros((self.maxlen,))
        else:
            if len(buffer)!=0:
                self.buffer=array(buffer).repeat(
                    ceil(self.maxlen/(len(buffer)*1.0)), 0)[:self.maxlen]
                self.pos=len(buffer)-1
            else:
                self.buffer=zeros((self.maxlen,)+buffer.shape[1:])

    def reset(self, buffer=None):
        '''Resets the buffer to the initial state
        '''
        self.pos=-1
        self.filled=False
        # self.buffer = buffer
        if buffer is None:
            self.buffer=zeros((self.maxlen,))
        else:
            if len(buffer)!=0:
                self.buffer=array(buffer).repeat(
                    ceil(self.maxlen/(len(buffer)*1.0)), 0)[:self.maxlen]
                self.pos=len(buffer)-1
            else:
                self.buffer=zeros((self.maxlen,)+buffer.shape[1:])

    def append(self, item, axis=None):
        '''Appends an element to the last position of the buffer
        '''
        new_pos=(self.pos+1)%self.maxlen
        if len(self.buffer)>=self.maxlen:
            if self.pos>=(self.maxlen-1):
                self.filled=True
            self.buffer[new_pos]=array(item).real
        else:
            self.buffer=append(self.buffer, item, axis=axis)
        self.pos=new_pos

    def array(self):
        '''returns an ordered array instead of the circular
        working version
        '''
        if self.filled:
            return r_[self.buffer[self.pos+1:], self.buffer[:self.pos+1]]
        else:
            return r_[self.buffer[:self.pos+1]]

    def copy_from(self, other):
        '''Add copy support
        '''
        if type(other)==type(array([])):
            self.buffer= other[-self.maxlen:]
            self.pos=len(self.buffer)-1
            self.filled=self.pos>=(self.maxlen-1)
        elif other.__class__==self.__class__:
            # Check if the buffer has been removed.
            if len(other.buffer)==0:
                self.__init__(other.maxlen, other.buffer)
            else:
                self.buffer=other.buffer.copy()
                self.maxlen=other.maxlen
                self.pos=other.pos
                try:
                    self.filled=other.filled
                except AttributeError:
                    self.filled=False
        else:
            raise TypeError('CircBuffer support only copying from CircBuffer'
                            ' and arrays.')

    def __len__(self):
        if self.filled:
            return len(self.buffer)
        else:
            return (self.pos>0)*self.pos

    def __getitem__(self, item):
        return self.array().__getitem__(item)
